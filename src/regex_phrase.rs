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
    if value.is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphabetic() || byte.is_ascii_whitespace())
    {
        return Some(Vec::new());
    }

    let token_count = pinyin_regex_phrase_token_count(value, dictionary).unwrap_or(0);
    let lower = value.to_ascii_lowercase();
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
    if value.is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphabetic() || byte.is_ascii_whitespace())
    {
        return None;
    }

    let lower = value.to_ascii_lowercase();
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

    if count == 0 { None } else { Some(count) }
}
