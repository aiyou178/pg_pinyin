use std::collections::HashMap;
use std::ops::Range;

const CJK_START: u32 = 0x4E00;
const CJK_END: u32 = 0x9FFF;
const CJK_LEN: usize = (CJK_END - CJK_START + 1) as usize;
const NO_ENTRY: u32 = u32::MAX;
const CJK_CHAR_TABLE_THRESHOLD: usize = 256;
const CJK_WORD_ROOT_THRESHOLD: usize = 64;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Span {
    offset: u32,
    len: u32,
}

impl Span {
    const NONE: Self = Self {
        offset: 0,
        len: NO_ENTRY,
    };

    #[inline]
    fn range(self) -> Range<usize> {
        let start = self.offset as usize;
        start..start + self.len as usize
    }

    #[inline]
    fn slice(self, arena: &str) -> Option<&str> {
        if self.len == NO_ENTRY {
            None
        } else {
            Some(&arena[self.range()])
        }
    }
}

fn append_first_reading(raw: &str, out: &mut String) {
    let mut first = true;
    for part in raw.split_whitespace() {
        if let Some(reading) = part.split('|').find(|piece| !piece.is_empty()) {
            if !first {
                out.push(' ');
            }
            first = false;
            if reading.bytes().all(|byte| !byte.is_ascii_uppercase()) {
                out.push_str(reading);
            } else {
                for ch in reading.chars() {
                    out.push(ch.to_ascii_lowercase());
                }
            }
        }
    }
}

fn push_first_reading(arena: &mut String, raw: &str) -> Span {
    let start = arena.len();
    append_first_reading(raw, arena);
    let len = arena.len() - start;
    let offset = u32::try_from(start).expect("dictionary value arena exceeded u32::MAX");
    let len = u32::try_from(len).expect("dictionary reading exceeded u32::MAX");
    assert!(len != NO_ENTRY, "dictionary reading exceeded limit");
    Span { offset, len }
}

pub fn first_reading(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    append_first_reading(raw, &mut out);
    out
}

#[derive(Default)]
pub struct CharDictionary {
    cjk: Box<[Span]>,
    extra: HashMap<char, Span>,
    arena: String,
}

pub struct CharDictionaryBuilder {
    entries: Vec<(char, Span)>,
    arena: String,
    cjk_count: usize,
}

impl CharDictionaryBuilder {
    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            entries: Vec::with_capacity(capacity),
            arena: String::with_capacity(capacity.saturating_mul(6)),
            cjk_count: 0,
        }
    }

    pub fn insert(&mut self, key: &str, raw_pinyin: &str) {
        let mut chars = key.chars();
        let Some(ch) = chars.next() else {
            return;
        };
        if chars.next().is_some() {
            return;
        }
        let span = push_first_reading(&mut self.arena, raw_pinyin);
        if (CJK_START..=CJK_END).contains(&(ch as u32)) {
            self.cjk_count += 1;
        }
        self.entries.push((ch, span));
    }

    pub fn build(mut self) -> CharDictionary {
        if self.entries.is_empty() {
            return CharDictionary::default();
        }
        let use_cjk = self.cjk_count >= CJK_CHAR_TABLE_THRESHOLD;
        let mut cjk = if use_cjk {
            vec![Span::NONE; CJK_LEN].into_boxed_slice()
        } else {
            Box::default()
        };
        let extra_capacity = if use_cjk {
            self.entries.len().saturating_sub(self.cjk_count)
        } else {
            self.entries.len()
        };
        let mut extra = HashMap::with_capacity(extra_capacity);

        for (ch, span) in self.entries {
            let code = ch as u32;
            if use_cjk && (CJK_START..=CJK_END).contains(&code) {
                cjk[(code - CJK_START) as usize] = span;
            } else {
                extra.insert(ch, span);
            }
        }
        extra.shrink_to_fit();
        self.arena.shrink_to_fit();
        CharDictionary {
            cjk,
            extra,
            arena: self.arena,
        }
    }
}

impl CharDictionary {
    pub fn from_pairs<'a>(pairs: impl IntoIterator<Item = (&'a str, &'a str)>) -> Self {
        let iter = pairs.into_iter();
        let (lower, _) = iter.size_hint();
        let mut builder = CharDictionaryBuilder::with_capacity(lower);
        for (key, value) in iter {
            builder.insert(key, value);
        }
        builder.build()
    }

    #[inline]
    pub fn get(&self, ch: char) -> Option<&str> {
        if !self.cjk.is_empty() {
            let code = ch as u32;
            if (CJK_START..=CJK_END).contains(&code) {
                return self.cjk[(code - CJK_START) as usize].slice(&self.arena);
            }
        }
        self.extra.get(&ch).and_then(|span| span.slice(&self.arena))
    }

    #[inline]
    pub fn contains_char(&self, ch: char) -> bool {
        self.get(ch).is_some()
    }
}

#[derive(Clone, Copy)]
struct TrieNode {
    value: Span,
    child_start: u32,
    child_len: u32,
}

impl TrieNode {
    const EMPTY: Self = Self {
        value: Span::NONE,
        child_start: 0,
        child_len: 0,
    };
}

#[derive(Clone, Copy)]
struct TrieEdge {
    ch: char,
    child: u32,
}

#[derive(Default)]
pub struct WordTrie {
    nodes: Vec<TrieNode>,
    edges: Vec<TrieEdge>,
    cjk_roots: Box<[u32]>,
    values: String,
}

#[derive(Clone, Copy)]
struct WordEntry {
    key: Span,
    value: Span,
    seq: u32,
}

pub struct WordTrieBuilder {
    entries: Vec<WordEntry>,
    key_arena: String,
    values: String,
}

impl WordTrieBuilder {
    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            entries: Vec::with_capacity(capacity),
            key_arena: String::with_capacity(capacity.saturating_mul(9)),
            values: String::with_capacity(capacity.saturating_mul(11)),
        }
    }

    pub fn insert(&mut self, word: &str, raw_pinyin: &str) {
        if word.is_empty() {
            return;
        }
        let key_start = self.key_arena.len();
        self.key_arena.push_str(word);
        let key_offset = u32::try_from(key_start).expect("word key arena exceeded u32::MAX");
        let key_len = u32::try_from(word.len()).expect("word key exceeded u32::MAX");
        assert!(key_len != NO_ENTRY, "word key exceeded limit");
        let value = push_first_reading(&mut self.values, raw_pinyin);
        let seq = u32::try_from(self.entries.len()).expect("word count exceeded u32::MAX");
        self.entries.push(WordEntry {
            key: Span {
                offset: key_offset,
                len: key_len,
            },
            value,
            seq,
        });
    }

    pub fn build(mut self) -> WordTrie {
        if self.entries.is_empty() {
            return WordTrie::default();
        }

        let key_arena = &self.key_arena;
        self.entries.sort_unstable_by(|left, right| {
            key_arena[left.key.range()]
                .cmp(&key_arena[right.key.range()])
                .then_with(|| left.seq.cmp(&right.seq))
        });

        // Preserve last-write-wins semantics when duplicate words are inserted.
        let mut write = 0;
        for read in 0..self.entries.len() {
            let is_last = read + 1 == self.entries.len()
                || key_arena[self.entries[read].key.range()]
                    != key_arena[self.entries[read + 1].key.range()];
            if is_last {
                self.entries[write] = self.entries[read];
                write += 1;
            }
        }
        self.entries.truncate(write);

        let estimated_nodes = self.entries.len().saturating_mul(3) / 2 + 1;
        let mut nodes = Vec::with_capacity(estimated_nodes);
        let mut edges = Vec::with_capacity(estimated_nodes);
        nodes.push(TrieNode::EMPTY);

        let mut queue: Vec<(u32, usize, usize, usize)> = Vec::with_capacity(estimated_nodes);
        queue.push((0, 0, self.entries.len(), 0));
        let mut head = 0;

        while head < queue.len() {
            let (node_idx, mut lo, hi, byte_offset) = queue[head];
            head += 1;

            if key_arena[self.entries[lo].key.range()].len() == byte_offset {
                nodes[node_idx as usize].value = self.entries[lo].value;
                lo += 1;
            }

            let child_start = u32::try_from(edges.len()).expect("word trie edge count overflow");
            let mut i = lo;
            while i < hi {
                let key_i = &key_arena[self.entries[i].key.range()];
                let ch = key_i[byte_offset..]
                    .chars()
                    .next()
                    .expect("trie suffix must be non-empty");
                let next_offset = byte_offset + ch.len_utf8();
                let ch_bytes = &key_i.as_bytes()[byte_offset..next_offset];

                let mut group_end = i + 1;
                while group_end < hi {
                    let key_g = &key_arena[self.entries[group_end].key.range()];
                    if key_g.len() >= next_offset
                        && &key_g.as_bytes()[byte_offset..next_offset] == ch_bytes
                    {
                        group_end += 1;
                    } else {
                        break;
                    }
                }

                let child_idx = u32::try_from(nodes.len()).expect("word trie node count overflow");
                nodes.push(TrieNode::EMPTY);
                edges.push(TrieEdge {
                    ch,
                    child: child_idx,
                });
                queue.push((child_idx, i, group_end, next_offset));
                i = group_end;
            }

            let node = &mut nodes[node_idx as usize];
            node.child_start = child_start;
            node.child_len = (edges.len() as u32) - child_start;
        }

        let root = nodes[0];
        let root_edges =
            &edges[root.child_start as usize..(root.child_start + root.child_len) as usize];
        let cjk_roots = if root_edges.len() >= CJK_WORD_ROOT_THRESHOLD {
            let mut table = vec![NO_ENTRY; CJK_LEN].into_boxed_slice();
            for edge in root_edges {
                let code = edge.ch as u32;
                if (CJK_START..=CJK_END).contains(&code) {
                    table[(code - CJK_START) as usize] = edge.child;
                }
            }
            table
        } else {
            Box::default()
        };

        nodes.shrink_to_fit();
        edges.shrink_to_fit();
        self.values.shrink_to_fit();

        WordTrie {
            nodes,
            edges,
            cjk_roots,
            values: self.values,
        }
    }
}

impl WordTrie {
    pub fn from_pairs<'a>(pairs: impl IntoIterator<Item = (&'a str, &'a str)>) -> Self {
        let iter = pairs.into_iter();
        let (lower, _) = iter.size_hint();
        let mut builder = WordTrieBuilder::with_capacity(lower);
        for (word, value) in iter {
            builder.insert(word, value);
        }
        builder.build()
    }

    #[inline]
    fn find_child(&self, node_idx: u32, ch: char) -> Option<u32> {
        if node_idx == 0 && !self.cjk_roots.is_empty() {
            let code = ch as u32;
            if (CJK_START..=CJK_END).contains(&code) {
                let child = self.cjk_roots[(code - CJK_START) as usize];
                return (child != NO_ENTRY).then_some(child);
            }
        }
        let node = self.nodes[node_idx as usize];
        let start = node.child_start as usize;
        let slice = &self.edges[start..start + node.child_len as usize];
        if slice.len() <= 8 {
            slice
                .iter()
                .find(|edge| edge.ch == ch)
                .map(|edge| edge.child)
        } else {
            slice
                .binary_search_by_key(&ch, |edge| edge.ch)
                .ok()
                .map(|idx| slice[idx].child)
        }
    }

    #[inline]
    fn longest_match<'a>(&'a self, tokens: &[Token<'_>]) -> Option<(usize, &'a str)> {
        if self.nodes.is_empty() {
            return None;
        }
        let mut node_idx = 0u32;
        let mut best: Option<(usize, &'a str)> = None;
        for (offset, token) in tokens.iter().enumerate() {
            let Token::Char(ch) = *token else {
                break;
            };
            let Some(child_idx) = self.find_child(node_idx, ch) else {
                break;
            };
            node_idx = child_idx;
            if let Some(reading) = self.nodes[node_idx as usize].value.slice(&self.values) {
                best = Some((offset + 1, reading));
            }
        }
        best
    }
}

#[derive(Clone, Copy)]
pub(crate) enum Token<'a> {
    Ascii(&'a str),
    Char(char),
}

#[inline]
fn han(ch: char) -> bool {
    matches!(ch, '\u{3400}'..='\u{4dbf}' | '\u{4e00}'..='\u{9fff}' |
        '\u{f900}'..='\u{faff}' | '\u{20000}'..='\u{323af}')
}

#[inline]
fn get_char<'a>(
    base: &'a CharDictionary,
    overlay: Option<&'a CharDictionary>,
    ch: char,
) -> Option<&'a str> {
    overlay.and_then(|ov| ov.get(ch)).or_else(|| base.get(ch))
}

#[inline]
fn contains_char(base: &CharDictionary, overlay: Option<&CharDictionary>, ch: char) -> bool {
    overlay.is_some_and(|ov| ov.contains_char(ch)) || base.contains_char(ch)
}

#[inline]
fn push_nonempty_token(out: &mut String, token: &str) {
    if token.is_empty() {
        return;
    }
    if !out.is_empty() {
        out.push(' ');
    }
    out.push_str(token);
}

#[inline]
fn push_ascii_lower_token(out: &mut String, token: &str) {
    if token.is_empty() {
        return;
    }
    if !out.is_empty() {
        out.push(' ');
    }
    if token.bytes().all(|byte| !byte.is_ascii_uppercase()) {
        out.push_str(token);
    } else {
        for byte in token.bytes() {
            out.push(byte.to_ascii_lowercase() as char);
        }
    }
}

fn tokenize_into<'a>(
    value: &'a str,
    base_chars: &CharDictionary,
    overlay_chars: Option<&CharDictionary>,
    out: &mut Vec<Token<'a>>,
) {
    let mut run = None;
    for (start, ch) in value.char_indices() {
        if ch.is_ascii_alphanumeric() {
            run.get_or_insert(start);
            continue;
        }
        if let Some(begin) = run.take() {
            out.push(Token::Ascii(&value[begin..start]));
        }
        if ch.is_whitespace() {
            continue;
        }
        if ch.is_ascii() {
            if contains_char(base_chars, overlay_chars, ch) {
                out.push(Token::Ascii(&value[start..start + 1]));
            }
        } else if han(ch) || contains_char(base_chars, overlay_chars, ch) {
            out.push(Token::Char(ch));
        }
    }
    if let Some(begin) = run {
        out.push(Token::Ascii(&value[begin..]));
    }
}

pub(crate) fn romanize_tokens(
    tokens: &[Token<'_>],
    base_chars: &CharDictionary,
    overlay_chars: Option<&CharDictionary>,
    base_words: &WordTrie,
    overlay_words: Option<&WordTrie>,
) -> String {
    let mut out = String::with_capacity(tokens.len().saturating_mul(5));
    let mut start = 0;
    while start < tokens.len() {
        match tokens[start] {
            Token::Ascii(token) => {
                push_ascii_lower_token(&mut out, token);
                start += 1;
            }
            Token::Char(ch) => {
                let mut best = base_words.longest_match(&tokens[start..]);
                if let Some(ov) = overlay_words
                    && let Some(ov_match) = ov.longest_match(&tokens[start..])
                    && best.is_none_or(|(base_len, _)| ov_match.0 >= base_len)
                {
                    best = Some(ov_match);
                }
                if let Some((length, reading)) = best {
                    push_nonempty_token(&mut out, reading);
                    start += length;
                } else {
                    if let Some(reading) = get_char(base_chars, overlay_chars, ch) {
                        push_nonempty_token(&mut out, reading);
                    }
                    start += 1;
                }
            }
        }
    }
    out
}

pub(crate) fn romanize_with_scratch<'a>(
    inputs: impl IntoIterator<Item = &'a str>,
    base_chars: &CharDictionary,
    overlay_chars: Option<&CharDictionary>,
    base_words: &WordTrie,
    overlay_words: Option<&WordTrie>,
    scratch: &mut Vec<Token<'a>>,
) -> String {
    scratch.clear();
    for input in inputs {
        tokenize_into(input, base_chars, overlay_chars, scratch);
    }
    romanize_tokens(
        scratch,
        base_chars,
        overlay_chars,
        base_words,
        overlay_words,
    )
}

pub fn romanize<'a>(
    inputs: impl IntoIterator<Item = &'a str>,
    base_chars: &CharDictionary,
    overlay_chars: Option<&CharDictionary>,
    base_words: &WordTrie,
    overlay_words: Option<&WordTrie>,
) -> String {
    let mut scratch = Vec::new();
    romanize_with_scratch(
        inputs,
        base_chars,
        overlay_chars,
        base_words,
        overlay_words,
        &mut scratch,
    )
}

pub fn romanize_chars(
    value: &str,
    base_chars: &CharDictionary,
    overlay_chars: Option<&CharDictionary>,
) -> String {
    let mut out = String::with_capacity(value.len());
    let mut run = None;
    for (start, ch) in value.char_indices() {
        if ch.is_ascii_alphanumeric() {
            run.get_or_insert(start);
            continue;
        }
        if let Some(begin) = run.take() {
            push_ascii_lower_token(&mut out, &value[begin..start]);
        }
        if ch.is_whitespace() {
            continue;
        }
        if ch.is_ascii() {
            if contains_char(base_chars, overlay_chars, ch) {
                push_ascii_lower_token(&mut out, &value[start..start + 1]);
            }
        } else if let Some(reading) = get_char(base_chars, overlay_chars, ch) {
            push_nonempty_token(&mut out, reading);
        }
    }
    if let Some(begin) = run {
        push_ascii_lower_token(&mut out, &value[begin..]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phrases_precede_singletons_and_keep_unmapped_han() {
        let chars = CharDictionary::from_pairs([("葛", "|ge|"), ("优", "|you|")]);
        let words = WordTrie::from_pairs([
            ("葛", "singleton"),
            ("葛优", "surname you"),
            ("𰻞优", "bian you"),
        ]);
        assert_eq!(
            romanize(["葛优ABC！"], &chars, None, &words, None),
            "surname you abc"
        );
        assert_eq!(
            romanize(["葛", "优ABC", "！"], &chars, None, &words, None),
            "surname you abc"
        );
        assert_eq!(romanize(["𰻞优"], &chars, None, &words, None), "bian you");
        assert_eq!(romanize(["𰻞!"], &chars, None, &words, None), "");
        assert_eq!(
            romanize(["ABC", "DEF"], &chars, None, &words, None),
            "abc def"
        );
        assert_eq!(romanize_chars("葛优ABC！", &chars, None), "ge you abc");
    }

    #[test]
    fn delta_overlay_matches_merged_longest_match_and_empty_override_semantics() {
        let base_chars =
            CharDictionary::from_pairs([("葛", "|ge|"), ("优", "|you|"), ("甲", "|jia|")]);
        let overlay_chars = CharDictionary::from_pairs([("葛", "|||"), ("优", "|YOU2|")]);
        let base_words = WordTrie::from_pairs([
            ("葛优", "base two"),
            ("葛优甲", "base three"),
            ("甲乙", "base jia yi"),
        ]);
        let overlay_words = WordTrie::from_pairs([("葛优", "overlay two"), ("甲乙", "   ")]);

        // Longer base phrase still wins over shorter overlay phrase.
        assert_eq!(
            romanize(
                ["葛优甲"],
                &base_chars,
                Some(&overlay_chars),
                &base_words,
                Some(&overlay_words)
            ),
            "base three"
        );
        // Equal-length overlay phrase overrides base phrase.
        assert_eq!(
            romanize(
                ["葛优"],
                &base_chars,
                Some(&overlay_chars),
                &base_words,
                Some(&overlay_words)
            ),
            "overlay two"
        );
        // Empty overlay word overrides base word without falling back to characters.
        assert_eq!(
            romanize(
                ["甲乙"],
                &base_chars,
                Some(&overlay_chars),
                &base_words,
                Some(&overlay_words)
            ),
            ""
        );
        // Empty overlay char overrides base char reading.
        assert_eq!(
            romanize_chars("葛优甲", &base_chars, Some(&overlay_chars)),
            "you2 jia"
        );
    }

    #[test]
    fn word_trie_handles_embedded_dictionary_and_duplicate_last_write_wins() {
        let mut chars_builder = CharDictionaryBuilder::with_capacity(512);
        for line in include_str!("../sql/data/pinyin_mapping.csv").lines() {
            if let Some((character, pinyin)) = line.trim_end_matches('\r').split_once(',') {
                chars_builder.insert(character, pinyin);
            }
        }
        let chars = chars_builder.build();

        let mut words_builder = WordTrieBuilder::with_capacity(512);
        for line in include_str!("../sql/data/pinyin_words.csv")
            .lines()
            .take(2000)
        {
            if let Some((word, pinyin)) = line.trim_end_matches('\r').split_once(',') {
                words_builder.insert(word, pinyin);
            }
        }
        words_builder.insert("葛优", "first");
        words_builder.insert("葛优", "second");
        let words = words_builder.build();

        assert_eq!(romanize(["葛优"], &chars, None, &words, None), "second");
        assert_eq!(romanize_chars("中国ABC", &chars, None), "zhong guo abc");
    }
}
