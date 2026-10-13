use std::collections::HashSet;

use unicode_normalization::UnicodeNormalization;

const MAX_INPUT_SCALARS: usize = 256;
const MAX_OUTPUT_TOKENS: usize = 64;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueryMode {
    Full,
    Initials,
    Autocomplete,
}

pub fn parse_mode(mode: &str) -> Result<QueryMode, String> {
    match mode {
        "full" => Ok(QueryMode::Full),
        "initials" => Ok(QueryMode::Initials),
        "autocomplete" => Ok(QueryMode::Autocomplete),
        _ => Err(format!(
            "unknown pinyin query mode {mode:?}; expected full, initials or autocomplete"
        )),
    }
}

pub struct PinyinQueryDictionary {
    syllables_by_first: [Vec<String>; 26],
}

impl PinyinQueryDictionary {
    pub fn from_token_csv(csv_text: &str) -> Self {
        let mut seen = HashSet::new();
        let mut syllables_by_first: [Vec<String>; 26] = std::array::from_fn(|_| Vec::new());

        for line in csv_text.lines() {
            let Some((token, category)) = line.trim_end_matches('\r').split_once(',') else {
                continue;
            };
            if category != "1" && category != "3" {
                continue;
            }
            let token = token.to_ascii_lowercase();
            if token.is_empty() || !token.bytes().all(|byte| byte.is_ascii_lowercase()) {
                continue;
            }
            if seen.insert(token.clone()) {
                syllables_by_first[(token.as_bytes()[0] - b'a') as usize].push(token);
            }
        }

        for syllable in ["m", "n"] {
            if seen.insert(syllable.to_string()) {
                syllables_by_first[(syllable.as_bytes()[0] - b'a') as usize]
                    .push(syllable.to_string());
            }
        }

        for bucket in &mut syllables_by_first {
            bucket
                .sort_by(|left, right| right.len().cmp(&left.len()).then_with(|| left.cmp(right)));
        }

        Self { syllables_by_first }
    }

    fn match_syllable_len(&self, value: &str) -> Option<usize> {
        let first = *value.as_bytes().first()?;
        self.syllables_by_first[(first - b'a') as usize]
            .iter()
            .find(|syllable| value.starts_with(syllable.as_str()))
            .map(String::len)
    }

    fn is_prefix(&self, value: &str) -> bool {
        let Some(&first) = value.as_bytes().first() else {
            return false;
        };
        self.syllables_by_first[(first - b'a') as usize]
            .iter()
            .any(|syllable| syllable.starts_with(value))
    }
}

#[derive(Default)]
struct Chunk {
    text: String,
    tone_positions: Vec<usize>,
    marked_syllabic_initial: bool,
    numeric_tone: bool,
}

struct LastLetter {
    position: usize,
    raw: char,
    diaeresis: bool,
    tone: bool,
}

fn normalize_chunks(value: &str) -> Option<Vec<Chunk>> {
    if value.chars().take(MAX_INPUT_SCALARS + 1).count() > MAX_INPUT_SCALARS {
        return None;
    }

    let mut chunks = Vec::new();
    let mut current = Chunk::default();
    let mut last_letter: Option<LastLetter> = None;
    let lower = value.to_lowercase();

    for character in lower.nfd() {
        match character {
            'a'..='z' => {
                let position = current.text.len();
                current.text.push(character);
                last_letter = Some(LastLetter {
                    position,
                    raw: character,
                    diaeresis: false,
                    tone: false,
                });
            }
            '\u{0308}' | ':' => {
                let last = last_letter.as_mut()?;
                if last.raw != 'u' || last.diaeresis || (character == ':' && last.tone) {
                    return None;
                }
                current
                    .text
                    .replace_range(last.position..=last.position, "v");
                last.diaeresis = true;
            }
            '\u{0300}' | '\u{0301}' | '\u{0304}' | '\u{030c}' => {
                let last = last_letter.as_mut()?;
                if last.tone || !matches!(last.raw, 'a' | 'e' | 'i' | 'o' | 'u' | 'v' | 'm' | 'n') {
                    return None;
                }
                last.tone = true;
                current.tone_positions.push(last.position);
                current.marked_syllabic_initial |= matches!(last.raw, 'm' | 'n');
            }
            '0'..='5' => {
                if current.text.is_empty() {
                    return None;
                }
                current.numeric_tone = true;
                chunks.push(std::mem::take(&mut current));
                last_letter = None;
            }
            '\'' | '\u{2018}' | '\u{2019}' => {
                if !current.text.is_empty() {
                    chunks.push(std::mem::take(&mut current));
                }
                last_letter = None;
            }
            separator if separator.is_whitespace() => {
                if !current.text.is_empty() {
                    chunks.push(std::mem::take(&mut current));
                }
                last_letter = None;
            }
            _ => return None,
        }
    }

    if !current.text.is_empty() {
        chunks.push(current);
    }
    if chunks
        .iter()
        .any(|chunk| chunk.marked_syllabic_initial && chunk.text != "m" && chunk.text != "n")
    {
        return None;
    }
    Some(chunks)
}

fn full_syllable_spans(
    chunk: &Chunk,
    dictionary: &PinyinQueryDictionary,
) -> Option<Vec<(usize, usize)>> {
    let mut spans = Vec::new();
    let mut position = 0;

    while position < chunk.text.len() {
        let length = dictionary.match_syllable_len(&chunk.text[position..])?;
        if tone_count(chunk, position, position + length) > 1 {
            return None;
        }
        spans.push((position, position + length));
        if spans.len() > MAX_OUTPUT_TOKENS {
            return None;
        }
        position += length;
    }

    if chunk.numeric_tone {
        let &(start, end) = spans.last()?;
        if tone_count(chunk, start, end) != 0 {
            return None;
        }
    }
    Some(spans)
}

fn tone_count(chunk: &Chunk, start: usize, end: usize) -> usize {
    chunk
        .tone_positions
        .iter()
        .filter(|&&position| start <= position && position < end)
        .count()
}

pub fn pinyin_regex_phrase_patterns(
    value: &str,
    mode: QueryMode,
    generated_pinyin: bool,
    dictionary: &PinyinQueryDictionary,
) -> Vec<String> {
    let Some(chunks) = normalize_chunks(value) else {
        return Vec::new();
    };
    let mut tokens: Vec<(String, bool)> = Vec::new();

    for (chunk_index, chunk) in chunks.iter().enumerate() {
        match mode {
            QueryMode::Full => {
                let Some(spans) = full_syllable_spans(chunk, dictionary) else {
                    return Vec::new();
                };
                tokens.extend(
                    spans
                        .into_iter()
                        .map(|(start, end)| (chunk.text[start..end].to_string(), false)),
                );
            }
            QueryMode::Initials => {
                if (!chunk.tone_positions.is_empty() || chunk.numeric_tone)
                    && full_syllable_spans(chunk, dictionary).is_none()
                {
                    return Vec::new();
                }
                let initials = if matches!(chunk.text.as_str(), "zh" | "ch" | "sh") {
                    vec![chunk.text.as_str()]
                } else {
                    (0..chunk.text.len())
                        .map(|index| &chunk.text[index..index + 1])
                        .collect()
                };
                for initial in initials {
                    if !dictionary.is_prefix(initial) {
                        return Vec::new();
                    }
                    tokens.push((initial.to_string(), true));
                }
            }
            QueryMode::Autocomplete => {
                let final_chunk = chunk_index + 1 == chunks.len();
                if !final_chunk || chunk.numeric_tone {
                    let Some(spans) = full_syllable_spans(chunk, dictionary) else {
                        return Vec::new();
                    };
                    tokens.extend(
                        spans
                            .into_iter()
                            .map(|(start, end)| (chunk.text[start..end].to_string(), false)),
                    );
                } else {
                    let mut position = 0;
                    while position < chunk.text.len() {
                        let remainder = &chunk.text[position..];
                        // Keep a valid final prefix whole: "yon" must not become "yo" + "n".
                        if dictionary.is_prefix(remainder) {
                            if tone_count(chunk, position, chunk.text.len()) > 1 {
                                return Vec::new();
                            }
                            tokens.push((remainder.to_string(), true));
                            position = chunk.text.len();
                        } else if let Some(length) = dictionary.match_syllable_len(remainder) {
                            if tone_count(chunk, position, position + length) > 1 {
                                return Vec::new();
                            }
                            tokens.push((remainder[..length].to_string(), false));
                            position += length;
                        } else {
                            return Vec::new();
                        }
                        if tokens.len() > MAX_OUTPUT_TOKENS {
                            return Vec::new();
                        }
                    }
                }
            }
        }

        if tokens.len() > MAX_OUTPUT_TOKENS {
            return Vec::new();
        }
    }

    if mode == QueryMode::Autocomplete {
        if let Some((_, prefix)) = tokens.last_mut() {
            *prefix = true;
        }
    }

    tokens
        .into_iter()
        .map(|(token, prefix)| match (generated_pinyin, prefix) {
            (false, false) => token,
            (false, true) => format!("{token}.*"),
            (true, false) => format!(r"({token}|.*\|{token}\|.*)"),
            (true, true) => format!(r"({token}.*|.*\|{token}[^|]*\|.*)"),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dictionary() -> PinyinQueryDictionary {
        PinyinQueryDictionary::from_token_csv(include_str!("../sql/data/pinyin_token.csv"))
    }

    fn patterns(value: &str, mode: QueryMode) -> Vec<String> {
        pinyin_regex_phrase_patterns(value, mode, false, &dictionary())
    }

    #[test]
    fn modes_are_explicit_and_case_sensitive() {
        assert_eq!(parse_mode("full"), Ok(QueryMode::Full));
        assert_eq!(parse_mode("initials"), Ok(QueryMode::Initials));
        assert_eq!(parse_mode("autocomplete"), Ok(QueryMode::Autocomplete));
        for invalid in ["", "compat", "Full", " full", "prefix"] {
            assert!(parse_mode(invalid).is_err());
        }
    }

    #[test]
    fn full_inventory_excludes_initials_and_includes_single_vowels_and_syllabic_mn() {
        let dictionary = PinyinQueryDictionary::from_token_csv(
            "an,1\na,3\nzh,2\nb,2\nm,2\nn,2\nan,1\ninvalid!,1\nx,4\n",
        );
        assert_eq!(
            pinyin_regex_phrase_patterns("a an m n", QueryMode::Full, false, &dictionary),
            ["a", "an", "m", "n"]
        );
        for invalid in ["zh", "b", "x", "invalid"] {
            assert!(
                pinyin_regex_phrase_patterns(invalid, QueryMode::Full, false, &dictionary)
                    .is_empty()
            );
        }
    }

    #[test]
    fn normalizes_umlaut_tones_case_and_numeric_boundaries() {
        for value in [
            "lv lve",
            "LV LVE",
            "l\u{00fc} l\u{00fc}e",
            "lu\u{0308} lu\u{0308}e",
            "lu: lu:e",
            "l\u{01dc} l\u{00fc}\u{0300}e",
            "lv4lve4",
            "LU:4LU:E0",
        ] {
            assert_eq!(patterns(value, QueryMode::Full), ["lv", "lve"], "{value}");
        }
        assert_eq!(
            patterns("n\u{01d0} h\u{01ce}o", QueryMode::Full),
            ["ni", "hao"]
        );
        assert_eq!(patterns("NI3HAO5", QueryMode::Full), ["ni", "hao"]);
        assert_eq!(patterns("ni0hao1", QueryMode::Full), ["ni", "hao"]);
    }

    #[test]
    fn explicit_boundaries_override_joined_greedy_ambiguity() {
        assert_eq!(patterns("xian", QueryMode::Full), ["xian"]);
        for value in ["xi an", "xi'an", "xi\u{2018}an", "xi\u{2019}an", "xi1an1"] {
            assert_eq!(patterns(value, QueryMode::Full), ["xi", "an"]);
        }
        assert_eq!(patterns("xingan", QueryMode::Full), ["xing", "an"]);
        assert_eq!(patterns("xin gan", QueryMode::Full), ["xin", "gan"]);
    }

    #[test]
    fn full_rejects_unfinished_initials_instead_of_falling_back() {
        for value in ["whh", "wuh", "zh", "wu h", "an g", "v"] {
            assert!(patterns(value, QueryMode::Full).is_empty(), "{value}");
        }
        assert_eq!(
            patterns("an ang lv lve", QueryMode::Full),
            ["an", "ang", "lv", "lve"]
        );
    }

    #[test]
    fn autocomplete_expands_only_the_last_token() {
        assert_eq!(patterns("wuhan", QueryMode::Autocomplete), ["wu", "han.*"]);
        assert_eq!(patterns("wu h", QueryMode::Autocomplete), ["wu", "h.*"]);
        assert_eq!(patterns("wuh", QueryMode::Autocomplete), ["wu", "h.*"]);
        assert_eq!(patterns("wu zh", QueryMode::Autocomplete), ["wu", "zh.*"]);
        assert_eq!(patterns("an", QueryMode::Autocomplete), ["an.*"]);
        assert_eq!(patterns("lv", QueryMode::Autocomplete), ["lv.*"]);
        assert_eq!(
            patterns("wu han   ", QueryMode::Autocomplete),
            ["wu", "han.*"]
        );
        assert_eq!(patterns("xi'an", QueryMode::Autocomplete), ["xi", "an.*"]);
        for value in ["whh", "h wu", "wu zh3", "zh3", "q7"] {
            assert!(
                patterns(value, QueryMode::Autocomplete).is_empty(),
                "{value}"
            );
        }
    }

    #[test]
    fn autocomplete_keeps_every_valid_final_syllable_prefix_whole() {
        let dictionary = dictionary();
        for syllable in dictionary.syllables_by_first.iter().flatten() {
            for end in 1..=syllable.len() {
                let prefix = &syllable[..end];
                assert_eq!(
                    pinyin_regex_phrase_patterns(
                        prefix,
                        QueryMode::Autocomplete,
                        false,
                        &dictionary
                    ),
                    [format!("{prefix}.*")],
                    "prefix {prefix} of {syllable}"
                );
                assert_eq!(
                    pinyin_regex_phrase_patterns(
                        prefix,
                        QueryMode::Autocomplete,
                        true,
                        &dictionary
                    ),
                    [format!(r"({prefix}.*|.*\|{prefix}[^|]*\|.*)")],
                    "generated prefix {prefix} of {syllable}"
                );
            }
        }
        for value in ["ni yon", "niyon"] {
            assert_eq!(patterns(value, QueryMode::Autocomplete), ["ni", "yon.*"]);
        }
        for value in ["ni lon", "nilon"] {
            assert_eq!(patterns(value, QueryMode::Autocomplete), ["ni", "lon.*"]);
        }
    }

    #[test]
    fn initials_split_compact_input_and_keep_explicit_digraph_chunks() {
        assert_eq!(patterns("whh", QueryMode::Initials), ["w.*", "h.*", "h.*"]);
        assert_eq!(patterns("WHH", QueryMode::Initials), ["w.*", "h.*", "h.*"]);
        assert_eq!(
            patterns("zh ch sh", QueryMode::Initials),
            ["zh.*", "ch.*", "sh.*"]
        );
        assert_eq!(
            patterns("zhchsh", QueryMode::Initials),
            ["z.*", "h.*", "c.*", "h.*", "s.*", "h.*"]
        );
        assert_eq!(
            patterns("zh'c s", QueryMode::Initials),
            ["zh.*", "c.*", "s.*"]
        );
        for invalid in ["v", "u", "i", "zh3"] {
            assert!(
                patterns(invalid, QueryMode::Initials).is_empty(),
                "{invalid}"
            );
        }
    }

    #[test]
    fn supports_syllabic_mn_without_treating_a_consonant_tone_as_a_vowel() {
        assert_eq!(patterns("m n", QueryMode::Full), ["m", "n"]);
        assert_eq!(patterns("\u{1e3f} \u{0144}", QueryMode::Full), ["m", "n"]);
        assert_eq!(patterns("m\u{0304} n\u{030c}", QueryMode::Full), ["m", "n"]);
        assert_eq!(patterns("m2 n3", QueryMode::Full), ["m", "n"]);
        assert_eq!(patterns("m\u{0301}", QueryMode::Initials), ["m.*"]);
        for invalid in ["m\u{0301}ma", "n\u{0301}i", "b\u{0301}"] {
            assert!(patterns(invalid, QueryMode::Full).is_empty(), "{invalid}");
        }
    }

    #[test]
    fn generated_exact_patterns_match_plain_terms_and_bounded_pipe_alternatives() {
        assert_eq!(
            pinyin_regex_phrase_patterns("an lv", QueryMode::Full, true, &dictionary()),
            [r"(an|.*\|an\|.*)", r"(lv|.*\|lv\|.*)"]
        );
        assert_eq!(
            pinyin_regex_phrase_patterns("an lv", QueryMode::Autocomplete, true, &dictionary()),
            [r"(an|.*\|an\|.*)", r"(lv.*|.*\|lv[^|]*\|.*)"]
        );
        assert_eq!(
            pinyin_regex_phrase_patterns("whh", QueryMode::Initials, true, &dictionary()),
            [
                r"(w.*|.*\|w[^|]*\|.*)",
                r"(h.*|.*\|h[^|]*\|.*)",
                r"(h.*|.*\|h[^|]*\|.*)"
            ]
        );
    }

    #[test]
    fn refuses_unsupported_characters_and_malformed_marks_without_silent_loss() {
        for value in [
            "\u{4f60}\u{597d}",
            "ni-hao",
            "ni_hao",
            "ni.hao",
            "ni/hao",
            "ni,hao",
            "ni6",
            "ni9",
            "3ni",
            "ni33",
            "ni3 3",
            "ni\u{0302}",
            "\u{0301}ni",
            "n\u{01d0}\u{0301}",
            "a\u{0308}",
            "u\u{0308}\u{0308}",
            "lu::",
            "lv:",
            "lu\u{0308}:",
            "l\u{00fc}:e",
            "lu\u{0301}:",
            "ni\0hao",
        ] {
            for mode in [
                QueryMode::Full,
                QueryMode::Initials,
                QueryMode::Autocomplete,
            ] {
                assert!(patterns(value, mode).is_empty(), "{value:?} {mode:?}");
            }
        }
    }

    #[test]
    fn numeric_tone_cannot_repeat_a_tone_in_the_last_syllable() {
        for value in ["n\u{01d0}3", "h\u{01ce}o3", "l\u{01dc}4"] {
            for mode in [
                QueryMode::Full,
                QueryMode::Initials,
                QueryMode::Autocomplete,
            ] {
                assert!(patterns(value, mode).is_empty(), "{value} {mode:?}");
            }
        }
        assert_eq!(patterns("n\u{01d0}hao3", QueryMode::Full), ["ni", "hao"]);
        assert_eq!(
            patterns("n\u{01d0}hao3", QueryMode::Autocomplete),
            ["ni", "hao.*"]
        );
    }

    #[test]
    fn tone_limit_applies_per_syllable_including_the_final_autocomplete_prefix() {
        for value in ["h\u{01ce}\u{00f3}", "\u{0101}\u{00ed}"] {
            for mode in [
                QueryMode::Full,
                QueryMode::Initials,
                QueryMode::Autocomplete,
            ] {
                assert!(patterns(value, mode).is_empty(), "{value} {mode:?}");
            }
        }
        assert_eq!(
            patterns("n\u{01d0}h\u{01ce}o", QueryMode::Full),
            ["ni", "hao"]
        );
        assert_eq!(
            patterns("n\u{01d0}h\u{01ce}o", QueryMode::Autocomplete),
            ["ni", "hao.*"]
        );
        assert_eq!(
            patterns("h\u{01ce}om\u{01ce}", QueryMode::Full),
            ["hao", "ma"]
        );
        assert_eq!(
            patterns("h\u{01ce}om\u{01ce}", QueryMode::Initials),
            ["h.*", "a.*", "o.*", "m.*", "a.*"]
        );

        let dictionary = PinyinQueryDictionary::from_token_csv("chuang,1\na,3\n");
        assert!(
            pinyin_regex_phrase_patterns(
                "ch\u{016b}\u{00e1}",
                QueryMode::Autocomplete,
                false,
                &dictionary,
            )
            .is_empty()
        );
        assert_eq!(
            pinyin_regex_phrase_patterns("ch\u{016b}", QueryMode::Autocomplete, false, &dictionary,),
            ["chu.*"]
        );
    }

    #[test]
    fn whitespace_and_apostrophes_are_explicit_unicode_boundaries() {
        for whitespace in [
            '\t', '\n', '\r', '\u{000b}', '\u{000c}', ' ', '\u{0085}', '\u{00a0}', '\u{1680}',
            '\u{2000}', '\u{200a}', '\u{2028}', '\u{2029}', '\u{202f}', '\u{205f}', '\u{3000}',
        ] {
            assert_eq!(
                patterns(&format!("xi{whitespace}an"), QueryMode::Full),
                ["xi", "an"]
            );
        }
        for empty in ["", "  \t\n", "'\u{2018}\u{2019}"] {
            assert!(patterns(empty, QueryMode::Full).is_empty());
        }
    }

    #[test]
    fn input_limit_counts_original_unicode_scalars_not_normalized_bytes() {
        let valid = format!("{}ni", " ".repeat(MAX_INPUT_SCALARS - 2));
        assert_eq!(patterns(&valid, QueryMode::Full), ["ni"]);
        let invalid = format!(" {valid}");
        assert!(patterns(&invalid, QueryMode::Full).is_empty());

        let composed = format!("{}l\u{00fc}", " ".repeat(MAX_INPUT_SCALARS - 2));
        assert_eq!(patterns(&composed, QueryMode::Full), ["lv"]);
        let decomposed = format!("{}lu\u{0308}", " ".repeat(MAX_INPUT_SCALARS - 3));
        assert_eq!(patterns(&decomposed, QueryMode::Full), ["lv"]);
    }

    #[test]
    fn output_limit_is_all_or_nothing_in_every_mode() {
        for mode in [
            QueryMode::Full,
            QueryMode::Initials,
            QueryMode::Autocomplete,
        ] {
            assert_eq!(
                patterns(&"a".repeat(MAX_OUTPUT_TOKENS), mode).len(),
                MAX_OUTPUT_TOKENS
            );
            assert!(patterns(&"a".repeat(MAX_OUTPUT_TOKENS + 1), mode).is_empty());
        }
        assert_eq!(
            patterns(&"a1".repeat(MAX_OUTPUT_TOKENS), QueryMode::Full).len(),
            MAX_OUTPUT_TOKENS
        );
        assert!(patterns(&"a1".repeat(MAX_OUTPUT_TOKENS + 1), QueryMode::Full).is_empty());
    }
}
