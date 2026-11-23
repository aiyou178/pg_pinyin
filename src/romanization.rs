use std::collections::{HashMap, HashSet};

#[derive(Default)]
pub struct WordIndex {
    prefixes: HashSet<String>,
}

impl WordIndex {
    pub fn new(words: &HashMap<String, String>) -> Self {
        let mut prefixes = HashSet::new();
        for word in words.keys() {
            for (end, _) in word.char_indices().skip(1) {
                prefixes.insert(word[..end].to_string());
            }
        }
        Self { prefixes }
    }
}

fn han(ch: char) -> bool {
    matches!(ch, '\u{3400}'..='\u{4dbf}' | '\u{4e00}'..='\u{9fff}' |
        '\u{f900}'..='\u{faff}' | '\u{20000}'..='\u{323af}')
}

fn tokens<'a>(value: &'a str, chars: &HashMap<String, String>) -> Vec<&'a str> {
    let mut result = Vec::new();
    let mut run = None;
    for (start, ch) in value.char_indices() {
        if ch.is_ascii_alphanumeric() {
            run.get_or_insert(start);
            continue;
        }
        if let Some(begin) = run.take() {
            result.push(&value[begin..start]);
        }
        let token = &value[start..start + ch.len_utf8()];
        if !ch.is_whitespace() && (han(ch) || chars.contains_key(token)) {
            result.push(token);
        }
    }
    if let Some(begin) = run {
        result.push(&value[begin..]);
    }
    result
}

pub fn first_reading(raw: &str) -> String {
    raw.split_whitespace()
        .filter_map(|part| part.split('|').find(|part| !part.is_empty()))
        .map(str::to_ascii_lowercase)
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn romanize<'a>(
    inputs: impl IntoIterator<Item = &'a str>,
    chars: &HashMap<String, String>,
    words: &HashMap<String, String>,
    index: &WordIndex,
) -> String {
    let tokens: Vec<_> = inputs
        .into_iter()
        .flat_map(|input| tokens(input, chars))
        .collect();
    let mut out = Vec::with_capacity(tokens.len());
    let mut start = 0;
    while start < tokens.len() {
        let token = tokens[start];
        if token.is_ascii() {
            out.push(token.to_ascii_lowercase());
            start += 1;
            continue;
        }
        let mut candidate = String::new();
        let mut best = None;
        for (offset, next) in tokens[start..].iter().enumerate() {
            if next.is_ascii() {
                break;
            }
            candidate.push_str(next);
            if let Some(mapped) = words.get(&candidate) {
                best = Some((offset + 1, mapped));
            }
            if !index.prefixes.contains(&candidate) {
                break;
            }
        }
        if let Some((length, mapped)) = best {
            out.push(first_reading(mapped));
            start += length;
        } else {
            if let Some(mapped) = chars.get(token) {
                out.push(first_reading(mapped));
            }
            start += 1;
        }
    }
    out.retain(|value| !value.is_empty());
    out.join(" ")
}

pub fn romanize_chars(value: &str, chars: &HashMap<String, String>) -> String {
    tokens(value, chars)
        .into_iter()
        .filter_map(|token| {
            if token.is_ascii() {
                Some(token.to_ascii_lowercase())
            } else {
                chars.get(token).map(|mapped| first_reading(mapped))
            }
        })
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phrases_precede_singletons_and_keep_unmapped_han() {
        let chars = HashMap::from([("葛".into(), "|ge|".into()), ("优".into(), "|you|".into())]);
        let words = HashMap::from([
            ("葛".into(), "singleton".into()),
            ("葛优".into(), "surname you".into()),
            ("𰻞优".into(), "bian you".into()),
        ]);
        let index = WordIndex::new(&words);
        assert_eq!(
            romanize(["葛优ABC！"], &chars, &words, &index),
            "surname you abc"
        );
        assert_eq!(
            romanize(["葛", "优ABC", "！"], &chars, &words, &index),
            "surname you abc"
        );
        assert_eq!(romanize(["𰻞优"], &chars, &words, &index), "bian you");
        assert_eq!(romanize(["𰻞!"], &chars, &words, &index), "");
        assert_eq!(romanize(["ABC", "DEF"], &chars, &words, &index), "abc def");
        assert_eq!(romanize_chars("葛优ABC！", &chars), "ge you abc");
    }
}
