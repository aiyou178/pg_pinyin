use std::collections::HashSet;

const EXTRA_INITIALS: [&str; 3] = ["zh", "ch", "sh"];

pub struct RegexTokenDictionary {
    tokens_by_first: [Vec<String>; 26],
    token_count: usize,
}

impl RegexTokenDictionary {
    pub fn from_tokens(tokens: impl IntoIterator<Item = String>) -> Self {
        let mut seen = HashSet::new();
        let mut tokens_by_first: [Vec<String>; 26] = std::array::from_fn(|_| Vec::new());

        for token in tokens {
            let token = token.to_ascii_lowercase();
            if token.is_empty() || !token.bytes().all(|byte| byte.is_ascii_lowercase()) {
                continue;
            }
            if !seen.insert(token.clone()) {
                continue;
            }

            let first = token.as_bytes()[0];
            tokens_by_first[(first - b'a') as usize].push(token);
        }

        for token in EXTRA_INITIALS {
            if seen.insert(token.to_string()) {
                tokens_by_first[(token.as_bytes()[0] - b'a') as usize].push(token.to_string());
            }
        }

        for bucket in &mut tokens_by_first {
            bucket.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a.cmp(b)));
        }

        Self {
            tokens_by_first,
            token_count: seen.len(),
        }
    }

    pub fn token_count(&self) -> usize {
        self.token_count
    }

    fn match_token_len(&self, lower: &str, idx: usize) -> usize {
        let byte = lower.as_bytes()[idx];
        let bucket = &self.tokens_by_first[(byte - b'a') as usize];
        bucket
            .iter()
            .find(|token| lower[idx..].starts_with(token.as_str()))
            .map(String::len)
            .unwrap_or(1)
    }
}

pub fn tokens_from_pinyin_token_csv(csv_text: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    for line in csv_text.lines() {
        let line = line.trim_end_matches('\r');
        if line.is_empty() {
            continue;
        }
        let Some((token, category)) = line.split_once(',') else {
            continue;
        };
        if category == "1" || EXTRA_INITIALS.contains(&token) {
            tokens.push(token.to_string());
        }
    }
    tokens
}

pub fn pinyin_regex_phrase_patterns(
    value: &str,
    generated_pinyin: bool,
    dictionary: &RegexTokenDictionary,
) -> Option<Vec<String>> {
    let Some(lower) = normalize_input(value) else {
        return Some(Vec::new());
    };

    let token_count = token_count_normalized(&lower, dictionary);
    let mut patterns = Vec::with_capacity(token_count);
    let mut idx = 0usize;

    while idx < lower.len() {
        let byte = lower.as_bytes()[idx];
        if byte.is_ascii_whitespace() {
            idx += 1;
            continue;
        }

        let token_len = dictionary.match_token_len(&lower, idx);
        let token = &lower[idx..idx + token_len];
        if generated_pinyin {
            let mut pattern = String::with_capacity(token.len() + 5);
            pattern.push_str(r".*\|");
            pattern.push_str(token);
            pattern.push_str(".*");
            patterns.push(pattern);
        } else {
            let mut pattern = String::with_capacity(token.len() + 2);
            pattern.push_str(token);
            pattern.push_str(".*");
            patterns.push(pattern);
        }
        idx += token_len;
    }

    Some(patterns)
}

pub fn pinyin_regex_phrase_token_count(
    value: &str,
    dictionary: &RegexTokenDictionary,
) -> Option<usize> {
    let lower = normalize_input(value)?;
    let count = token_count_normalized(&lower, dictionary);
    if count == 0 { None } else { Some(count) }
}

fn normalize_input(value: &str) -> Option<String> {
    if value.is_empty() {
        return None;
    }

    if value
        .bytes()
        .all(|byte| byte.is_ascii_alphabetic() || byte.is_ascii_whitespace())
    {
        return Some(value.to_ascii_lowercase());
    }

    let mut lower = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\'' | '\u{2018}' | '\u{2019}' => lower.push(' '),
            ch if ch.is_ascii_alphabetic() || ch.is_ascii_whitespace() => {
                lower.push(ch.to_ascii_lowercase());
            }
            _ => return None,
        }
    }
    Some(lower)
}

fn token_count_normalized(lower: &str, dictionary: &RegexTokenDictionary) -> usize {
    let mut count = 0usize;
    let mut idx = 0usize;

    while idx < lower.len() {
        let byte = lower.as_bytes()[idx];
        if byte.is_ascii_whitespace() {
            idx += 1;
            continue;
        }

        let token_len = dictionary.match_token_len(&lower, idx);
        count += 1;
        idx += token_len;
    }

    count
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dictionary() -> RegexTokenDictionary {
        RegexTokenDictionary::from_tokens(
            [
                "xian", "xi", "an", "xin", "gan", "xing", "ni", "hao", "chang", "zhong", "guo",
                "bei", "jing", "shang", "hai",
            ]
            .into_iter()
            .map(str::to_string),
        )
    }

    fn patterns(value: &str, generated_pinyin: bool) -> Vec<String> {
        pinyin_regex_phrase_patterns(value, generated_pinyin, &dictionary()).unwrap()
    }

    #[test]
    fn apostrophe_preserves_an_explicit_syllable_boundary() {
        assert_eq!(patterns("xian", false), ["xian.*"]);
        assert_eq!(patterns("xi'an", false), ["xi.*", "an.*"]);
        assert_eq!(patterns("XI'AN", false), ["xi.*", "an.*"]);
    }

    #[test]
    fn smart_apostrophes_preserve_the_same_boundary() {
        for value in ["xi\u{2018}an", "xi\u{2019}an", "XI\u{2019}AN"] {
            assert_eq!(patterns(value, false), ["xi.*", "an.*"]);
        }
    }

    #[test]
    fn apostrophe_prevents_matching_across_longer_token_prefixes() {
        assert_eq!(patterns("xingan", false), ["xing.*", "an.*"]);
        assert_eq!(patterns("xin'gan", false), ["xin.*", "gan.*"]);
        assert_eq!(patterns("xin\u{2019}gan", false), ["xin.*", "gan.*"]);
    }

    #[test]
    fn apostrophes_combine_with_existing_whitespace_boundaries() {
        for value in [
            "xi an",
            "'xi'an'",
            "''xi'''an''",
            " \t'XI' \r\n'AN'\t ",
            "\u{2018}xi'\u{2019}an\u{2019}",
        ] {
            assert_eq!(patterns(value, false), ["xi.*", "an.*"]);
        }
    }

    #[test]
    fn separator_only_input_has_no_patterns_or_token_count() {
        for value in ["", "'", "'''", " '\t\r\n' ", "\u{2018}\u{2019}"] {
            assert!(patterns(value, false).is_empty());
            assert_eq!(pinyin_regex_phrase_token_count(value, &dictionary()), None);
        }
    }

    #[test]
    fn apostrophes_keep_initials_and_greedy_matching() {
        assert_eq!(patterns("whh", false), ["w.*", "h.*", "h.*"]);
        assert_eq!(patterns("w'h'h", false), ["w.*", "h.*", "h.*"]);
        assert_eq!(patterns("zh'ch'sh", false), ["zh.*", "ch.*", "sh.*"]);
        assert_eq!(patterns("zhong'guo", false), ["zhong.*", "guo.*"]);
    }

    #[test]
    fn generated_patterns_keep_the_existing_pipe_prefix() {
        for value in ["xi'an", "xi\u{2018}an", "xi\u{2019}an"] {
            assert_eq!(patterns(value, true), [r".*\|xi.*", r".*\|an.*"]);
        }
        assert_eq!(patterns("whh", true), [r".*\|w.*", r".*\|h.*", r".*\|h.*"]);
    }

    #[test]
    fn unsupported_characters_still_reject_the_whole_input() {
        for value in [
            "xi'an1",
            "xi'\u{0101}n",
            "l\u{00fc}'an",
            "xi'an!",
            "xi-an",
            "xi_an",
            "xi\"an",
            "xi`an",
            "xi\u{02bc}an",
            "xi\u{00a0}an",
            "xi\u{3000}an",
            "xi\x0ban",
            "xi'an\0",
            "xi'an\u{897f}",
        ] {
            assert!(patterns(value, false).is_empty(), "{value:?}");
            assert!(patterns(value, true).is_empty(), "{value:?}");
            assert_eq!(pinyin_regex_phrase_token_count(value, &dictionary()), None);
        }
    }

    #[test]
    fn token_count_matches_patterns_for_every_supported_separator() {
        for separator in ["'", "\u{2018}", "\u{2019}", " ", "\t", "\n", "\r", "\x0c"] {
            for chunks in [["xi", "an"], ["w", "h"], ["zhong", "guo"], ["xing", "an"]] {
                let value = format!(
                    "{separator}{}{separator}{}{separator}",
                    chunks[0], chunks[1]
                );
                for generated in [false, true] {
                    let actual = patterns(&value, generated);
                    assert_eq!(
                        pinyin_regex_phrase_token_count(&value, &dictionary()),
                        Some(actual.len()),
                        "{value:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn old_ascii_behavior_matches_the_same_inventory() {
        let dictionary = RegexTokenDictionary::from_tokens(tokens_from_pinyin_token_csv(
            include_str!("../sql/data/pinyin_token.csv"),
        ));
        let mut inputs = [
            "",
            " ",
            "whh",
            "XIAn",
            "xingan",
            "beijing",
            "shanghai",
            "zhongguo",
            "ni hao",
            "\tchang\r\nan\x0b\x0c",
        ]
        .into_iter()
        .map(str::to_string)
        .collect::<Vec<_>>();
        let alphabet = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ \t\r\n\x0b\x0c";
        let mut seed = 0x1234_5678_u32;
        for len in 0..96 {
            let mut input = String::with_capacity(len);
            for _ in 0..len {
                seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                input.push(char::from(alphabet[seed as usize % alphabet.len()]));
            }
            inputs.push(input);
        }

        for input in inputs {
            let lower = input.to_ascii_lowercase();
            let mut legacy_tokens = Vec::new();
            let mut idx = 0;
            let valid = input
                .bytes()
                .all(|byte| byte.is_ascii_alphabetic() || byte.is_ascii_whitespace());
            while valid && idx < lower.len() {
                if lower.as_bytes()[idx].is_ascii_whitespace() {
                    idx += 1;
                    continue;
                }
                let len = dictionary.match_token_len(&lower, idx);
                legacy_tokens.push(&lower[idx..idx + len]);
                idx += len;
            }
            let legacy_count = if legacy_tokens.is_empty() {
                None
            } else {
                Some(legacy_tokens.len())
            };
            assert_eq!(
                pinyin_regex_phrase_token_count(&input, &dictionary),
                legacy_count,
                "{input:?}"
            );
            for generated in [false, true] {
                let expected = legacy_tokens
                    .iter()
                    .map(|token| {
                        if generated {
                            format!(r".*\|{token}.*")
                        } else {
                            format!("{token}.*")
                        }
                    })
                    .collect::<Vec<_>>();
                assert_eq!(
                    pinyin_regex_phrase_patterns(&input, generated, &dictionary),
                    Some(expected),
                    "{input:?}"
                );
            }
        }
    }
}
