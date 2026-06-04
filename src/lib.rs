#[cfg(feature = "extension")]
pgrx::pg_module_magic!();

pub mod regex_phrase;

#[cfg(feature = "extension")]
mod extension {
    use crate::regex_phrase::{self, RegexTokenDictionary};

    use std::collections::{BTreeMap, HashMap};
    use std::fs;
    use std::mem;
    use std::process;
    use std::sync::{Arc, OnceLock, RwLock};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[cfg(feature = "g2pm")]
    use ndarray::{Array1, Array2};
    #[cfg(feature = "hybrid_onnx")]
    use std::collections::HashSet;
    #[cfg(feature = "g2pm")]
    use std::path::{Path, PathBuf};
    #[cfg(feature = "hybrid_onnx")]
    use std::sync::Mutex;

    use pgrx::JsonB;
    use pgrx::datum::AnyElement;
    use pgrx::prelude::*;
    use pgrx::{GucContext, GucFlags, GucRegistry, GucSetting};
    use serde_json::{Value, json};

    const DICTIONARY_SCHEMA: &str = "pinyin";
    const DEFAULT_MIN_CONFIDENCE: f32 = 0.80;
    const DEFAULT_MIN_MARGIN: f32 = 0.05;
    const DEFAULT_DISABLE_ON_ERROR: bool = true;
    #[cfg(feature = "hybrid_onnx")]
    const DEFAULT_G2PW_WINDOW_SIZE: usize = 32;
    #[cfg(feature = "hybrid_onnx")]
    const DEFAULT_G2PW_INTRA_THREADS: usize = 1;
    const GUC_USE_MODEL_DEFAULT_I32: i32 = 0;
    const GUC_USE_MODEL_DEFAULT_F64: f64 = -1.0;

    static G2PW_WINDOW_SIZE_GUC: GucSetting<i32> =
        GucSetting::<i32>::new(GUC_USE_MODEL_DEFAULT_I32);
    static G2PW_INTRA_THREADS_GUC: GucSetting<i32> =
        GucSetting::<i32>::new(GUC_USE_MODEL_DEFAULT_I32);
    static MODEL_MIN_CONFIDENCE_GUC: GucSetting<f64> =
        GucSetting::<f64>::new(GUC_USE_MODEL_DEFAULT_F64);
    static MODEL_MIN_MARGIN_GUC: GucSetting<f64> =
        GucSetting::<f64>::new(GUC_USE_MODEL_DEFAULT_F64);

    const EMBEDDED_MAPPING_CSV: &str = include_str!("../sql/data/pinyin_mapping.csv");
    const EMBEDDED_TOKEN_CSV: &str = include_str!("../sql/data/pinyin_token.csv");
    const EMBEDDED_WORDS_CSV: &str = include_str!("../sql/data/pinyin_words.csv");

    #[derive(Default)]
    struct CharDictionaryCache {
        version: i64,
        loaded: bool,
        char_map: HashMap<String, String>,
    }

    #[derive(Default)]
    struct DictionaryCache {
        version: i64,
        loaded: bool,
        char_map: HashMap<String, String>,
        word_map: HashMap<String, String>,
        max_word_len: usize,
    }

    #[derive(Default)]
    struct TokenDictionaryCache {
        version: i64,
        loaded: bool,
        regex_tokens: Option<RegexTokenDictionary>,
    }

    #[derive(Default)]
    struct SuffixDictionaryCacheEntry {
        base_version: i64,
        char_map: HashMap<String, String>,
        overlay_char_map: HashMap<String, String>,
        words_loaded: bool,
        word_map: HashMap<String, String>,
        overlay_word_map: HashMap<String, String>,
        max_word_len: usize,
    }

    #[derive(Clone, Debug)]
    struct ModelSpec {
        model_name: String,
        kind: String,
        #[cfg(feature = "g2pm")]
        model_path: String,
        #[cfg(feature = "hybrid_onnx")]
        tokenizer_path: Option<String>,
        #[cfg(feature = "hybrid_onnx")]
        labels_path: Option<String>,
        config: Value,
    }

    #[derive(Clone, Debug)]
    struct ModelConfig {
        min_confidence: f32,
        min_margin: f32,
        disable_on_error: bool,
    }

    #[cfg(feature = "hybrid_onnx")]
    #[derive(Clone, Debug, Eq, PartialEq)]
    struct G2pwLoadConfig {
        window_size: usize,
        intra_op_num_threads: usize,
    }

    impl Default for ModelConfig {
        fn default() -> Self {
            Self {
                min_confidence: DEFAULT_MIN_CONFIDENCE,
                min_margin: DEFAULT_MIN_MARGIN,
                disable_on_error: DEFAULT_DISABLE_ON_ERROR,
            }
        }
    }

    #[derive(Clone, Debug)]
    struct ModelRequest {
        sentence: String,
        target_char_offsets: Vec<usize>,
        candidate_sets: Vec<Vec<String>>,
    }

    #[derive(Clone, Debug)]
    struct ModelDecision {
        chosen: String,
        confidence: f32,
        margin: f32,
    }

    #[derive(Clone, Debug)]
    struct MockDecision {
        chosen: String,
        confidence: f32,
        margin: f32,
    }

    trait PolyphoneModel: Send + Sync {
        fn disambiguate(&self, req: &ModelRequest) -> Result<Vec<ModelDecision>, String>;
    }

    #[derive(Clone)]
    struct ConfigOnlyModel {
        decisions: HashMap<String, MockDecision>,
    }

    impl PolyphoneModel for ConfigOnlyModel {
        fn disambiguate(&self, req: &ModelRequest) -> Result<Vec<ModelDecision>, String> {
            let chars: Vec<char> = req.sentence.chars().collect();
            let mut out = Vec::with_capacity(req.target_char_offsets.len());
            for (idx, offset) in req.target_char_offsets.iter().enumerate() {
                let key = chars
                    .get(*offset)
                    .map(|ch| ch.to_string())
                    .ok_or_else(|| format!("mock decision offset {offset} out of range"))?;
                let decision = self.decisions.get(&key).ok_or_else(|| {
                    let candidates = req
                        .candidate_sets
                        .get(idx)
                        .cloned()
                        .unwrap_or_default()
                        .join(",");
                    format!(
                        "no mock decision configured for '{key}' with candidates [{candidates}]"
                    )
                })?;
                out.push(ModelDecision {
                    chosen: decision.chosen.clone(),
                    confidence: decision.confidence,
                    margin: decision.margin,
                });
            }
            Ok(out)
        }
    }

    #[cfg(feature = "hybrid_onnx")]
    #[derive(Clone)]
    struct WordPieceTokenizer {
        vocab: HashMap<String, i64>,
        unk_token: String,
        cls_token: String,
        sep_token: String,
    }

    #[cfg(feature = "hybrid_onnx")]
    impl WordPieceTokenizer {
        fn load(path: &Path) -> Result<Self, String> {
            let vocab_text = fs::read_to_string(path).map_err(|err| {
                format!("failed to read tokenizer vocab '{}': {err}", path.display())
            })?;
            let mut vocab = HashMap::new();
            for (idx, line) in vocab_text.lines().enumerate() {
                let token = line.trim_end_matches('\r').trim();
                if token.is_empty() {
                    continue;
                }
                vocab.insert(token.to_string(), idx as i64);
            }

            for required in ["[UNK]", "[CLS]", "[SEP]"] {
                if !vocab.contains_key(required) {
                    return Err(format!(
                        "tokenizer vocab '{}' is missing required token {required}",
                        path.display()
                    ));
                }
            }

            Ok(Self {
                vocab,
                unk_token: "[UNK]".to_string(),
                cls_token: "[CLS]".to_string(),
                sep_token: "[SEP]".to_string(),
            })
        }

        fn token_to_id(&self, token: &str) -> Result<i64, String> {
            self.vocab.get(token).copied().ok_or_else(|| {
                format!("tokenizer vocab is missing token '{token}' required during inference")
            })
        }

        fn tokenize_word(&self, word: &str) -> Vec<String> {
            if word.is_empty() {
                return Vec::new();
            }

            if word.chars().count() == 1 && !word.chars().all(|ch| ch.is_ascii_alphanumeric()) {
                return if self.vocab.contains_key(word) {
                    vec![word.to_string()]
                } else {
                    vec![self.unk_token.clone()]
                };
            }

            if !word.is_ascii() {
                return if self.vocab.contains_key(word) {
                    vec![word.to_string()]
                } else {
                    vec![self.unk_token.clone()]
                };
            }

            let lowered = word.to_ascii_lowercase();
            let chars: Vec<char> = lowered.chars().collect();
            let mut start = 0usize;
            let mut out = Vec::new();

            while start < chars.len() {
                let mut end = chars.len();
                let mut found = None;
                while end > start {
                    let piece: String = chars[start..end].iter().collect();
                    let candidate = if start == 0 {
                        piece
                    } else {
                        format!("##{piece}")
                    };
                    if self.vocab.contains_key(&candidate) {
                        found = Some(candidate);
                        break;
                    }
                    end -= 1;
                }

                match found {
                    Some(token) => {
                        out.push(token);
                        start = end;
                    }
                    None => return vec![self.unk_token.clone()],
                }
            }

            out
        }
    }

    #[cfg(feature = "hybrid_onnx")]
    struct G2pwOnnxModel {
        session: Mutex<ort::session::Session>,
        labels: Vec<String>,
        normalized_labels: Vec<String>,
        char_to_label_indices: HashMap<String, Vec<usize>>,
        char_to_id: HashMap<String, i64>,
        tokenizer: WordPieceTokenizer,
        s2t_map: HashMap<char, char>,
        window_size: usize,
    }

    #[cfg(feature = "hybrid_onnx")]
    impl G2pwOnnxModel {
        fn normalize_bopomofo_base(
            bopomofo_map: &HashMap<String, String>,
            base: &str,
        ) -> Result<String, String> {
            if let Some(mapped) = bopomofo_map.get(base) {
                return Ok(mapped.clone());
            }

            // g2pW exposes a handful of non-canonical zhuyin spellings that are absent from the
            // shipped support table. Map them to the closest toneless pinyin so model outputs can
            // still be compared against our dictionary candidates.
            let fallback = match base {
                "ㄈㄨㄥ" => "feng",
                "ㄉㄧㄤ" => "dang",
                "ㄌㄩㄢ" => "luan",
                "ㄌㄩㄣ" => "lin",
                "ㄍㄧ" => "qian",
                "ㄝ" => "ei",
                "ㄩㄤ" => "yang",
                _ => {
                    return Err(format!(
                        "bopomofo label '{base}' is missing in support mapping"
                    ));
                }
            };

            Ok(fallback.to_string())
        }

        fn resolve_vocab_path(path: Option<&str>, model_dir: &Path) -> Result<PathBuf, String> {
            let Some(path) = path else {
                return Err(
                    "g2pw_onnx requires tokenizer_path pointing to a vocab.txt file or a directory containing vocab.txt"
                        .to_string(),
                );
            };

            let raw = PathBuf::from(path);
            if raw.is_file() {
                return Ok(raw);
            }
            if raw.is_dir() {
                let candidate = raw.join("vocab.txt");
                if candidate.exists() {
                    return Ok(candidate);
                }
            }

            let model_relative = model_dir.join(path);
            if model_relative.is_file() {
                return Ok(model_relative);
            }
            if model_relative.is_dir() {
                let candidate = model_relative.join("vocab.txt");
                if candidate.exists() {
                    return Ok(candidate);
                }
            }

            Err(format!(
                "tokenizer_path '{}' does not resolve to vocab.txt",
                path
            ))
        }

        fn resolve_support_file(
            explicit_path: Option<&str>,
            base_dir: &Path,
            fallback_name: &str,
        ) -> Result<PathBuf, String> {
            if let Some(path) = explicit_path {
                let raw = PathBuf::from(path);
                if raw.exists() {
                    return Ok(raw);
                }
                let joined = base_dir.join(path);
                if joined.exists() {
                    return Ok(joined);
                }
                return Err(format!("support file '{}' does not exist", path));
            }

            let candidate = base_dir.join(fallback_name);
            if candidate.exists() {
                Ok(candidate)
            } else {
                Err(format!(
                    "support file '{}' was not found under '{}'",
                    fallback_name,
                    base_dir.display()
                ))
            }
        }

        fn load_s2t_map(path: &Path) -> Result<HashMap<char, char>, String> {
            let text = fs::read_to_string(path)
                .map_err(|err| format!("failed to read s2t map '{}': {err}", path.display()))?;
            let mut out = HashMap::new();
            for line in text.lines() {
                let line = line.trim_end_matches('\r').trim();
                if line.is_empty() {
                    continue;
                }
                let Some((simplified, traditional)) = line.split_once('\t') else {
                    continue;
                };
                let mut s_chars = simplified.chars();
                let mut t_chars = traditional.chars();
                if let (Some(s), Some(t), None, None) = (
                    s_chars.next(),
                    t_chars.next(),
                    s_chars.next(),
                    t_chars.next(),
                ) {
                    out.insert(s, t);
                }
            }
            Ok(out)
        }

        fn load_bopomofo_map(path: &Path) -> Result<HashMap<String, String>, String> {
            let text = fs::read_to_string(path).map_err(|err| {
                format!(
                    "failed to read bopomofo support file '{}': {err}",
                    path.display()
                )
            })?;
            let value: Value = serde_json::from_str(&text).map_err(|err| {
                format!(
                    "failed to parse bopomofo support file '{}': {err}",
                    path.display()
                )
            })?;
            let mut out = HashMap::new();
            let Some(obj) = value.as_object() else {
                return Err(format!(
                    "bopomofo support file '{}' must contain a JSON object",
                    path.display()
                ));
            };
            for (key, raw_value) in obj {
                if let Some(value) = raw_value.as_str() {
                    out.insert(key.clone(), value.to_ascii_lowercase());
                }
            }
            Ok(out)
        }

        fn load_polyphonic_labels(
            path: &Path,
            bopomofo_map: &HashMap<String, String>,
        ) -> Result<
            (
                Vec<String>,
                Vec<String>,
                HashMap<String, Vec<usize>>,
                HashMap<String, i64>,
            ),
            String,
        > {
            let text = fs::read_to_string(path).map_err(|err| {
                format!(
                    "failed to read polyphonic labels file '{}': {err}",
                    path.display()
                )
            })?;
            let mut entries = Vec::new();
            for line in text.lines() {
                let line = line.trim_end_matches('\r').trim();
                if line.is_empty() {
                    continue;
                }
                let Some((ch, phoneme)) = line.split_once('\t') else {
                    return Err(format!(
                        "invalid polyphonic labels row '{}' in '{}'",
                        line,
                        path.display()
                    ));
                };
                entries.push((ch.to_string(), phoneme.to_string()));
            }

            let mut label_set = HashSet::new();
            for (_, phoneme) in &entries {
                label_set.insert(phoneme.clone());
            }
            let mut labels: Vec<String> = label_set.into_iter().collect();
            labels.sort();

            let mut normalized_labels = Vec::with_capacity(labels.len());
            for label in &labels {
                let (body, tone) = label.split_at(label.len().saturating_sub(1));
                let normalized = Self::normalize_bopomofo_base(bopomofo_map, body)?;
                if tone.is_empty() || !tone.chars().all(|ch| ch.is_ascii_digit()) {
                    return Err(format!("invalid tone suffix in bopomofo label '{label}'"));
                }
                normalized_labels.push(normalized);
            }

            let label_to_idx: HashMap<String, usize> = labels
                .iter()
                .enumerate()
                .map(|(idx, label)| (label.clone(), idx))
                .collect();
            let mut char_to_label_indices: HashMap<String, Vec<usize>> = HashMap::new();
            for (ch, phoneme) in entries {
                let idx = *label_to_idx.get(&phoneme).ok_or_else(|| {
                    format!("label '{phoneme}' missing from normalized label list")
                })?;
                char_to_label_indices.entry(ch).or_default().push(idx);
            }

            let mut chars: Vec<String> = char_to_label_indices.keys().cloned().collect();
            chars.sort();
            let char_to_id = chars
                .into_iter()
                .enumerate()
                .map(|(idx, ch)| (ch, idx as i64))
                .collect();

            Ok((labels, normalized_labels, char_to_label_indices, char_to_id))
        }

        fn wordize_and_map(text: &str) -> (Vec<String>, Vec<Option<usize>>, Vec<(usize, usize)>) {
            let mut words = Vec::new();
            let mut text_to_word = Vec::new();
            let mut word_to_text = Vec::new();
            let chars: Vec<char> = text.chars().collect();
            let mut idx = 0usize;

            while idx < chars.len() {
                if chars[idx] == ' ' {
                    while idx < chars.len() && chars[idx] == ' ' {
                        text_to_word.push(None);
                        idx += 1;
                    }
                    continue;
                }

                if chars[idx].is_ascii_alphanumeric() {
                    let start = idx;
                    while idx < chars.len() && chars[idx].is_ascii_alphanumeric() {
                        idx += 1;
                    }
                    let word: String = chars[start..idx].iter().collect();
                    let word_index = words.len();
                    words.push(word);
                    word_to_text.push((start, idx));
                    text_to_word.extend(std::iter::repeat_n(Some(word_index), idx - start));
                    continue;
                }

                let start = idx;
                idx += 1;
                let word_index = words.len();
                words.push(chars[start].to_string());
                word_to_text.push((start, idx));
                text_to_word.push(Some(word_index));
            }

            (words, text_to_word, word_to_text)
        }

        fn tokenize_and_map(
            tokenizer: &WordPieceTokenizer,
            text: &str,
        ) -> (Vec<String>, Vec<Option<usize>>, Vec<(usize, usize)>) {
            let (words, text_to_word, word_to_text) = Self::wordize_and_map(text);
            let mut tokens = Vec::new();
            let mut token_to_text = Vec::new();

            for (word, (start, end)) in words.iter().zip(word_to_text.iter()) {
                let word_tokens = tokenizer.tokenize_word(word);
                if word_tokens.len() == 1 && word_tokens[0] == tokenizer.unk_token {
                    tokens.push(tokenizer.unk_token.clone());
                    token_to_text.push((*start, *end));
                    continue;
                }

                let mut cursor = *start;
                for word_token in word_tokens {
                    let stripped = word_token.trim_start_matches("##");
                    let token_len = stripped.chars().count();
                    token_to_text.push((cursor, cursor + token_len));
                    cursor += token_len;
                    tokens.push(word_token);
                }
            }

            let mut text_to_token = text_to_word;
            for (token_idx, (start, end)) in token_to_text.iter().enumerate() {
                for pos in *start..*end {
                    if let Some(slot) = text_to_token.get_mut(pos) {
                        *slot = Some(token_idx);
                    }
                }
            }

            (tokens, text_to_token, token_to_text)
        }

        fn truncate_text_window(
            text: &str,
            query_id: usize,
            window_size: usize,
        ) -> (String, usize) {
            let chars: Vec<char> = text.chars().collect();
            let start = query_id.saturating_sub(window_size / 2);
            let end = usize::min(chars.len(), query_id + window_size / 2);
            (
                chars[start..end].iter().collect(),
                query_id.saturating_sub(start),
            )
        }

        fn truncate_tokens(
            max_len: usize,
            text: String,
            query_id: usize,
            tokens: Vec<String>,
            text_to_token: Vec<Option<usize>>,
            token_to_text: Vec<(usize, usize)>,
        ) -> Result<
            (
                String,
                usize,
                Vec<String>,
                Vec<Option<usize>>,
                Vec<(usize, usize)>,
            ),
            String,
        > {
            let truncate_len = max_len.saturating_sub(2);
            if tokens.len() <= truncate_len {
                return Ok((text, query_id, tokens, text_to_token, token_to_text));
            }

            let Some(token_position) = text_to_token.get(query_id).and_then(|v| *v) else {
                return Err(format!(
                    "query position {query_id} could not be mapped to a tokenizer token"
                ));
            };

            let mut token_start = token_position.saturating_sub(truncate_len / 2);
            let mut token_end = token_start + truncate_len;
            if token_end > tokens.len() {
                let overflow = token_end - tokens.len();
                token_start = token_start.saturating_sub(overflow);
                token_end = token_start + truncate_len;
            }

            let start = token_to_text[token_start].0;
            let end = token_to_text[token_end - 1].1;
            let trimmed_text: String = text.chars().skip(start).take(end - start).collect();
            let trimmed_query = query_id - start;
            let trimmed_tokens = tokens[token_start..token_end].to_vec();
            let trimmed_text_to_token = text_to_token[start..end]
                .iter()
                .map(|value| value.map(|token_idx| token_idx - token_start))
                .collect();
            let trimmed_token_to_text = token_to_text[token_start..token_end]
                .iter()
                .map(|(s, e)| (s - start, e - start))
                .collect();

            Ok((
                trimmed_text,
                trimmed_query,
                trimmed_tokens,
                trimmed_text_to_token,
                trimmed_token_to_text,
            ))
        }

        fn build_feature(
            &self,
            sentence: &str,
            query_id: usize,
        ) -> Result<(Vec<i64>, Vec<i64>, Vec<i64>, Vec<f32>, i64, i64, String), String> {
            let translated: String = sentence
                .chars()
                .map(|ch| self.s2t_map.get(&ch).copied().unwrap_or(ch))
                .collect::<String>()
                .to_lowercase();
            let (text, query_id) =
                Self::truncate_text_window(&translated, query_id, self.window_size);
            let (tokens, text_to_token, token_to_text) =
                Self::tokenize_and_map(&self.tokenizer, &text);
            let (text, query_id, tokens, text_to_token, _) =
                Self::truncate_tokens(512, text, query_id, tokens, text_to_token, token_to_text)?;
            let query_char = text
                .chars()
                .nth(query_id)
                .ok_or_else(|| format!("query offset {query_id} out of range after truncation"))?
                .to_string();
            let processed_tokens = std::iter::once(self.tokenizer.cls_token.as_str())
                .chain(tokens.iter().map(String::as_str))
                .chain(std::iter::once(self.tokenizer.sep_token.as_str()));

            let mut input_ids = Vec::new();
            for token in processed_tokens {
                input_ids.push(self.tokenizer.token_to_id(token)?);
            }
            let token_type_ids = vec![0_i64; input_ids.len()];
            let attention_mask = vec![1_i64; input_ids.len()];
            let mut phoneme_mask = vec![0_f32; self.labels.len()];
            if let Some(indices) = self.char_to_label_indices.get(&query_char) {
                for idx in indices {
                    phoneme_mask[*idx] = 1.0;
                }
            } else {
                return Err(format!(
                    "query character '{}' is not present in model polyphonic labels",
                    query_char
                ));
            }
            let char_id = *self.char_to_id.get(&query_char).ok_or_else(|| {
                format!(
                    "query character '{}' is missing from model char id map",
                    query_char
                )
            })?;
            let position_id = text_to_token
                .get(query_id)
                .and_then(|value| *value)
                .map(|token_idx| token_idx as i64 + 1)
                .ok_or_else(|| {
                    format!(
                        "query position {} could not be mapped to a token in '{}'",
                        query_id, text
                    )
                })?;

            Ok((
                input_ids,
                token_type_ids,
                attention_mask,
                phoneme_mask,
                char_id,
                position_id,
                query_char,
            ))
        }

        fn load(spec: &ModelSpec, load_config: &G2pwLoadConfig) -> Result<Self, String> {
            let model_path = PathBuf::from(&spec.model_path);
            if !model_path.exists() {
                return Err(format!(
                    "model file '{}' does not exist; provide operator-managed g2pW assets",
                    spec.model_path
                ));
            }
            let model_dir = model_path
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| PathBuf::from("."));
            let vocab_path = Self::resolve_vocab_path(spec.tokenizer_path.as_deref(), &model_dir)?;
            let tokenizer = WordPieceTokenizer::load(&vocab_path)?;
            let support_dir = vocab_path
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| model_dir.clone());
            let s2t_path =
                Self::resolve_support_file(None, &support_dir, "bert-base-chinese_s2t_dict.txt")?;
            let bopomofo_path = Self::resolve_support_file(
                None,
                &support_dir,
                "bopomofo_to_pinyin_wo_tune_dict.json",
            )?;
            let bopomofo_map = Self::load_bopomofo_map(&bopomofo_path)?;
            let labels_path = spec
                .labels_path
                .as_deref()
                .map(PathBuf::from)
                .unwrap_or_else(|| model_dir.join("POLYPHONIC_CHARS.txt"));
            let (labels, normalized_labels, char_to_label_indices, char_to_id) =
                Self::load_polyphonic_labels(&labels_path, &bopomofo_map)?;
            let s2t_map = Self::load_s2t_map(&s2t_path)?;

            let session = ort::session::Session::builder()
                .map_err(|err| format!("failed to create ONNX session builder: {err}"))?
                .with_intra_threads(load_config.intra_op_num_threads)
                .map_err(|err| format!("failed to set ONNX intra threads: {err}"))?
                .commit_from_file(&model_path)
                .map_err(|err| {
                    format!(
                        "failed to load ONNX model '{}': {err}",
                        model_path.display()
                    )
                })?;

            Ok(Self {
                session: Mutex::new(session),
                labels,
                normalized_labels,
                char_to_label_indices,
                char_to_id,
                tokenizer,
                s2t_map,
                window_size: load_config.window_size,
            })
        }
    }

    #[cfg(feature = "hybrid_onnx")]
    impl PolyphoneModel for G2pwOnnxModel {
        fn disambiguate(&self, req: &ModelRequest) -> Result<Vec<ModelDecision>, String> {
            use ort::session::SessionInputValue;
            use ort::value::Tensor;

            if req.target_char_offsets.len() != req.candidate_sets.len() {
                return Err(
                    "model request candidate count does not match target offsets".to_string(),
                );
            }

            let mut features = Vec::with_capacity(req.target_char_offsets.len());
            let mut query_chars = Vec::with_capacity(req.target_char_offsets.len());
            for offset in &req.target_char_offsets {
                let built = self.build_feature(&req.sentence, *offset)?;
                query_chars.push(built.6.clone());
                features.push(built);
            }

            let batch_size = features.len();
            let max_seq_len = features
                .iter()
                .map(|feature| feature.0.len())
                .max()
                .unwrap_or(0);
            let mut input_ids = vec![0_i64; batch_size * max_seq_len];
            let mut token_type_ids = vec![0_i64; batch_size * max_seq_len];
            let mut attention_mask = vec![0_i64; batch_size * max_seq_len];
            let mut phoneme_mask = vec![0_f32; batch_size * self.labels.len()];
            let mut char_ids = Vec::with_capacity(batch_size);
            let mut position_ids = Vec::with_capacity(batch_size);

            for (row_idx, feature) in features.iter().enumerate() {
                for (col_idx, value) in feature.0.iter().enumerate() {
                    input_ids[row_idx * max_seq_len + col_idx] = *value;
                }
                for (col_idx, value) in feature.1.iter().enumerate() {
                    token_type_ids[row_idx * max_seq_len + col_idx] = *value;
                }
                for (col_idx, value) in feature.2.iter().enumerate() {
                    attention_mask[row_idx * max_seq_len + col_idx] = *value;
                }
                for (col_idx, value) in feature.3.iter().enumerate() {
                    phoneme_mask[row_idx * self.labels.len() + col_idx] = *value;
                }
                char_ids.push(feature.4);
                position_ids.push(feature.5);
            }

            let inputs: Vec<(String, SessionInputValue<'static>)> = vec![
                (
                    "input_ids".to_string(),
                    Tensor::from_array(([batch_size, max_seq_len], input_ids))
                        .map_err(|err| format!("failed building input_ids tensor: {err}"))?
                        .into_dyn()
                        .into(),
                ),
                (
                    "token_type_ids".to_string(),
                    Tensor::from_array(([batch_size, max_seq_len], token_type_ids))
                        .map_err(|err| format!("failed building token_type_ids tensor: {err}"))?
                        .into_dyn()
                        .into(),
                ),
                (
                    "attention_mask".to_string(),
                    Tensor::from_array(([batch_size, max_seq_len], attention_mask))
                        .map_err(|err| format!("failed building attention_mask tensor: {err}"))?
                        .into_dyn()
                        .into(),
                ),
                (
                    "phoneme_mask".to_string(),
                    Tensor::from_array(([batch_size, self.labels.len()], phoneme_mask))
                        .map_err(|err| format!("failed building phoneme_mask tensor: {err}"))?
                        .into_dyn()
                        .into(),
                ),
                (
                    "char_ids".to_string(),
                    Tensor::from_array(([batch_size], char_ids))
                        .map_err(|err| format!("failed building char_ids tensor: {err}"))?
                        .into_dyn()
                        .into(),
                ),
                (
                    "position_ids".to_string(),
                    Tensor::from_array(([batch_size], position_ids))
                        .map_err(|err| format!("failed building position_ids tensor: {err}"))?
                        .into_dyn()
                        .into(),
                ),
            ];

            let mut session = self
                .session
                .lock()
                .map_err(|_| "g2pw_onnx session lock poisoned".to_string())?;
            let outputs = session
                .run(inputs)
                .map_err(|err| format!("failed running ONNX inference: {err}"))?;
            let probs = outputs[0]
                .try_extract_array::<f32>()
                .map_err(|err| format!("failed extracting ONNX output tensor: {err}"))?;

            let mut out = Vec::with_capacity(batch_size);
            for row_idx in 0..batch_size {
                let query_char = &query_chars[row_idx];
                let char_indices = self
                    .char_to_label_indices
                    .get(query_char)
                    .ok_or_else(|| format!("query char '{}' missing label indices", query_char))?;
                let row = probs.index_axis(ndarray::Axis(0), row_idx);
                let candidate_set = &req.candidate_sets[row_idx];
                let mut scored = Vec::new();

                for candidate in candidate_set {
                    let mut best = f32::NEG_INFINITY;
                    for label_idx in char_indices {
                        if self.normalized_labels[*label_idx] == *candidate {
                            best = best.max(row[*label_idx]);
                        }
                    }
                    if best.is_finite() {
                        scored.push((candidate.clone(), best));
                    }
                }

                if scored.is_empty() {
                    let fallback = candidate_set
                        .first()
                        .cloned()
                        .unwrap_or_else(|| query_char.clone());
                    out.push(ModelDecision {
                        chosen: fallback,
                        confidence: 0.0,
                        margin: 0.0,
                    });
                    continue;
                }

                scored.sort_by(|a, b| b.1.total_cmp(&a.1));
                let chosen = scored[0].0.clone();
                let confidence = scored[0].1;
                let margin = if scored.len() >= 2 {
                    confidence - scored[1].1
                } else {
                    confidence
                };
                out.push(ModelDecision {
                    chosen,
                    confidence,
                    margin,
                });
            }

            Ok(out)
        }
    }

    #[cfg(feature = "g2pm")]
    struct G2pmNumpyModel {
        char2idx: HashMap<String, usize>,
        label_buckets: HashMap<String, Vec<usize>>,
        embeddings: Array2<f32>,
        weight_ih: Array2<f32>,
        weight_hh: Array2<f32>,
        bias_ih: Array1<f32>,
        bias_hh: Array1<f32>,
        weight_ih_reverse: Array2<f32>,
        weight_hh_reverse: Array2<f32>,
        bias_ih_reverse: Array1<f32>,
        bias_hh_reverse: Array1<f32>,
        hidden_weight_l0: Array2<f32>,
        hidden_bias_l0: Array1<f32>,
        hidden_weight_l1: Array2<f32>,
        hidden_bias_l1: Array1<f32>,
        unk_id: usize,
        bos_id: usize,
        eos_id: usize,
    }

    #[cfg(feature = "g2pm")]
    struct G2pmTensorSpec {
        path: PathBuf,
        shape: Vec<usize>,
        offset_bytes: usize,
        byte_length: Option<usize>,
    }

    #[cfg(feature = "g2pm")]
    impl G2pmNumpyModel {
        fn resolve_manifest_path(model_path: &str) -> Result<PathBuf, String> {
            let raw = PathBuf::from(model_path);
            if raw.is_file() {
                return Ok(raw);
            }
            if raw.is_dir() {
                let candidate = raw.join("manifest.json");
                if candidate.exists() {
                    return Ok(candidate);
                }
            }

            Err(format!(
                "g2pm_numpy requires model_path pointing to a manifest.json file or a directory containing manifest.json: '{}'",
                model_path
            ))
        }

        fn read_manifest(path: &Path) -> Result<Value, String> {
            let text = fs::read_to_string(path).map_err(|err| {
                format!("failed to read g2pm manifest '{}': {err}", path.display())
            })?;
            let value: Value = serde_json::from_str(&text).map_err(|err| {
                format!("failed to parse g2pm manifest '{}': {err}", path.display())
            })?;
            let format = value
                .get("format")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if format != "g2pm_export_v1" && format != "g2pm_export_v2" {
                return Err(format!(
                    "unsupported g2pm manifest format '{}' in '{}'; expected 'g2pm_export_v1' or 'g2pm_export_v2'",
                    format,
                    path.display()
                ));
            }
            Ok(value)
        }

        fn manifest_object<'a>(
            value: &'a Value,
            field: &str,
            manifest_path: &Path,
        ) -> Result<&'a serde_json::Map<String, Value>, String> {
            value.get(field).and_then(Value::as_object).ok_or_else(|| {
                format!(
                    "g2pm manifest '{}' must contain object field '{}'",
                    manifest_path.display(),
                    field
                )
            })
        }

        fn parse_char2idx(
            value: &Value,
            manifest_path: &Path,
        ) -> Result<HashMap<String, usize>, String> {
            let obj = Self::manifest_object(value, "char2idx", manifest_path)?;
            let mut out = HashMap::with_capacity(obj.len());
            for (ch, raw_idx) in obj {
                let Some(idx) = raw_idx.as_u64() else {
                    return Err(format!(
                        "g2pm manifest '{}' has non-integer char2idx value for '{}'",
                        manifest_path.display(),
                        ch
                    ));
                };
                out.insert(ch.clone(), idx as usize);
            }
            Ok(out)
        }

        fn parse_idx2class(value: &Value, manifest_path: &Path) -> Result<Vec<String>, String> {
            let raw = value
                .get("idx2class")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    format!(
                        "g2pm manifest '{}' must contain array field 'idx2class'",
                        manifest_path.display()
                    )
                })?;
            let mut out = Vec::with_capacity(raw.len());
            for (idx, item) in raw.iter().enumerate() {
                let Some(label) = item.as_str() else {
                    return Err(format!(
                        "g2pm manifest '{}' has non-string idx2class entry at index {}",
                        manifest_path.display(),
                        idx
                    ));
                };
                out.push(label.to_string());
            }
            Ok(out)
        }

        fn manifest_format<'a>(value: &'a Value, manifest_path: &Path) -> Result<&'a str, String> {
            value.get("format").and_then(Value::as_str).ok_or_else(|| {
                format!(
                    "g2pm manifest '{}' must contain string field 'format'",
                    manifest_path.display()
                )
            })
        }

        fn packed_weights_path(value: &Value, manifest_path: &Path) -> Result<PathBuf, String> {
            let file = value
                .get("weights_path")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    format!(
                        "g2pm manifest '{}' must contain string field 'weights_path' for format 'g2pm_export_v2'",
                        manifest_path.display()
                    )
                })?;

            let base_dir = manifest_path
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| PathBuf::from("."));
            let packed_path = base_dir.join(file);
            if !packed_path.exists() {
                return Err(format!(
                    "g2pm packed weights path '{}' does not exist",
                    packed_path.display()
                ));
            }
            Ok(packed_path)
        }

        fn parse_tensor_spec(
            value: &Value,
            manifest_path: &Path,
            field: &str,
            expected_dims: usize,
        ) -> Result<G2pmTensorSpec, String> {
            let tensors = Self::manifest_object(value, "tensors", manifest_path)?;
            let entry = tensors.get(field).ok_or_else(|| {
                format!(
                    "g2pm manifest '{}' is missing tensor '{}'",
                    manifest_path.display(),
                    field
                )
            })?;
            let entry_obj = entry.as_object().ok_or_else(|| {
                format!(
                    "g2pm manifest '{}' tensor '{}' must be an object",
                    manifest_path.display(),
                    field
                )
            })?;
            let shape_value = entry_obj
                .get("shape")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    format!(
                        "g2pm manifest '{}' tensor '{}' must contain array field 'shape'",
                        manifest_path.display(),
                        field
                    )
                })?;
            if shape_value.len() != expected_dims {
                return Err(format!(
                    "g2pm tensor '{}' in '{}' expected {} dimensions, got {}",
                    field,
                    manifest_path.display(),
                    expected_dims,
                    shape_value.len()
                ));
            }
            let mut shape = Vec::with_capacity(shape_value.len());
            for dim in shape_value {
                let Some(value) = dim.as_u64() else {
                    return Err(format!(
                        "g2pm tensor '{}' in '{}' has non-integer shape component",
                        field,
                        manifest_path.display()
                    ));
                };
                shape.push(value as usize);
            }

            let format = Self::manifest_format(value, manifest_path)?;
            if format == "g2pm_export_v2" {
                let offset_bytes =
                    entry_obj
                        .get("offset_bytes")
                        .and_then(Value::as_u64)
                        .ok_or_else(|| {
                            format!(
                                "g2pm manifest '{}' tensor '{}' must contain integer field 'offset_bytes'",
                                manifest_path.display(),
                                field
                            )
                        })? as usize;
                let byte_length =
                    entry_obj
                        .get("byte_length")
                        .and_then(Value::as_u64)
                        .ok_or_else(|| {
                            format!(
                                "g2pm manifest '{}' tensor '{}' must contain integer field 'byte_length'",
                                manifest_path.display(),
                                field
                            )
                        })? as usize;
                Ok(G2pmTensorSpec {
                    path: Self::packed_weights_path(value, manifest_path)?,
                    shape,
                    offset_bytes,
                    byte_length: Some(byte_length),
                })
            } else {
                let file = entry_obj
                    .get("path")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        format!(
                            "g2pm manifest '{}' tensor '{}' must contain string field 'path'",
                            manifest_path.display(),
                            field
                        )
                    })?;
                let base_dir = manifest_path
                    .parent()
                    .map(Path::to_path_buf)
                    .unwrap_or_else(|| PathBuf::from("."));
                let tensor_path = base_dir.join(file);
                if !tensor_path.exists() {
                    return Err(format!(
                        "g2pm tensor '{}' path '{}' does not exist",
                        field,
                        tensor_path.display()
                    ));
                }
                Ok(G2pmTensorSpec {
                    path: tensor_path,
                    shape,
                    offset_bytes: 0,
                    byte_length: None,
                })
            }
        }

        fn read_f32_bin(
            path: &Path,
            offset_bytes: usize,
            byte_length: Option<usize>,
            expected_len: usize,
        ) -> Result<Vec<f32>, String> {
            let bytes = fs::read(path)
                .map_err(|err| format!("failed to read g2pm tensor '{}': {err}", path.display()))?;
            if offset_bytes > bytes.len() {
                return Err(format!(
                    "g2pm tensor '{}' offset {} is beyond file length {}",
                    path.display(),
                    offset_bytes,
                    bytes.len()
                ));
            }
            let end = match byte_length {
                Some(len) => offset_bytes.checked_add(len).ok_or_else(|| {
                    format!(
                        "g2pm tensor '{}' offset {} + length {} overflowed",
                        path.display(),
                        offset_bytes,
                        len
                    )
                })?,
                None => bytes.len(),
            };
            if end > bytes.len() {
                return Err(format!(
                    "g2pm tensor '{}' range {}..{} exceeds file length {}",
                    path.display(),
                    offset_bytes,
                    end,
                    bytes.len()
                ));
            }
            let slice = &bytes[offset_bytes..end];
            if slice.len() % 4 != 0 {
                return Err(format!(
                    "g2pm tensor '{}' byte length {} is not divisible by 4",
                    path.display(),
                    slice.len()
                ));
            }
            let actual_len = slice.len() / 4;
            if actual_len != expected_len {
                return Err(format!(
                    "g2pm tensor '{}' expected {} f32 values, found {}",
                    path.display(),
                    expected_len,
                    actual_len
                ));
            }

            let mut out = Vec::with_capacity(actual_len);
            for chunk in slice.chunks_exact(4) {
                out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
            }
            Ok(out)
        }

        fn load_array1(
            value: &Value,
            manifest_path: &Path,
            field: &str,
        ) -> Result<Array1<f32>, String> {
            let spec = Self::parse_tensor_spec(value, manifest_path, field, 1)?;
            let expected_len = spec.shape[0];
            let data = Self::read_f32_bin(
                &spec.path,
                spec.offset_bytes,
                spec.byte_length,
                expected_len,
            )?;
            Ok(Array1::from_vec(data))
        }

        fn load_array2(
            value: &Value,
            manifest_path: &Path,
            field: &str,
        ) -> Result<Array2<f32>, String> {
            let spec = Self::parse_tensor_spec(value, manifest_path, field, 2)?;
            let expected_len = spec.shape[0] * spec.shape[1];
            let data = Self::read_f32_bin(
                &spec.path,
                spec.offset_bytes,
                spec.byte_length,
                expected_len,
            )?;
            Array2::from_shape_vec((spec.shape[0], spec.shape[1]), data).map_err(|err| {
                format!(
                    "failed to construct g2pm tensor '{}' from '{}': {err}",
                    field,
                    spec.path.display()
                )
            })
        }

        fn sigmoid(value: f32) -> f32 {
            1.0 / (1.0 + (-value).exp())
        }

        fn relu(value: f32) -> f32 {
            if value > 0.0 { value } else { 0.0 }
        }

        fn lstm_step(
            input: ndarray::ArrayView1<'_, f32>,
            prev_h: &Array1<f32>,
            prev_c: &Array1<f32>,
            weight_ih: &Array2<f32>,
            weight_hh: &Array2<f32>,
            bias_ih: &Array1<f32>,
            bias_hh: &Array1<f32>,
        ) -> (Array1<f32>, Array1<f32>) {
            let ifgo_ih = weight_ih.dot(&input) + bias_ih;
            let ifgo_hh = weight_hh.dot(prev_h) + bias_hh;
            let gates = ifgo_ih + ifgo_hh;
            let hidden = prev_h.len();

            let mut next_h = Array1::<f32>::zeros(hidden);
            let mut next_c = Array1::<f32>::zeros(hidden);
            for idx in 0..hidden {
                let input_gate = Self::sigmoid(gates[idx]);
                let forget_gate = Self::sigmoid(gates[hidden + idx]);
                let candidate = (gates[2 * hidden + idx]).tanh();
                let output_gate = Self::sigmoid(gates[3 * hidden + idx]);
                let cell = forget_gate * prev_c[idx] + input_gate * candidate;
                let hidden_value = output_gate * cell.tanh();
                next_c[idx] = cell;
                next_h[idx] = hidden_value;
            }
            (next_h, next_c)
        }

        fn sentence_input_ids(&self, sentence: &str) -> Vec<usize> {
            let mut input_ids = Vec::with_capacity(sentence.chars().count() + 2);
            input_ids.push(self.bos_id);
            for ch in sentence.chars() {
                let key = ch.to_string();
                input_ids.push(*self.char2idx.get(&key).unwrap_or(&self.unk_id));
            }
            input_ids.push(self.eos_id);
            input_ids
        }

        fn forward_hidden_states(&self, input_ids: &[usize]) -> Result<Vec<Array1<f32>>, String> {
            let hidden_size = self.weight_hh.shape()[1];
            let mut prev_h = Array1::<f32>::zeros(hidden_size);
            let mut prev_c = Array1::<f32>::zeros(hidden_size);
            let mut out = Vec::with_capacity(input_ids.len());

            for id in input_ids {
                if *id >= self.embeddings.shape()[0] {
                    return Err(format!(
                        "g2pm input id {} is out of range for embedding rows {}",
                        id,
                        self.embeddings.shape()[0]
                    ));
                }
                let embedding = self.embeddings.row(*id);
                let (next_h, next_c) = Self::lstm_step(
                    embedding,
                    &prev_h,
                    &prev_c,
                    &self.weight_ih,
                    &self.weight_hh,
                    &self.bias_ih,
                    &self.bias_hh,
                );
                out.push(next_h.clone());
                prev_h = next_h;
                prev_c = next_c;
            }

            Ok(out)
        }

        fn backward_hidden_states(&self, input_ids: &[usize]) -> Result<Vec<Array1<f32>>, String> {
            let hidden_size = self.weight_hh_reverse.shape()[1];
            let mut prev_h = Array1::<f32>::zeros(hidden_size);
            let mut prev_c = Array1::<f32>::zeros(hidden_size);
            let mut reversed = Vec::with_capacity(input_ids.len());

            for id in input_ids.iter().rev() {
                if *id >= self.embeddings.shape()[0] {
                    return Err(format!(
                        "g2pm input id {} is out of range for embedding rows {}",
                        id,
                        self.embeddings.shape()[0]
                    ));
                }
                let embedding = self.embeddings.row(*id);
                let (next_h, next_c) = Self::lstm_step(
                    embedding,
                    &prev_h,
                    &prev_c,
                    &self.weight_ih_reverse,
                    &self.weight_hh_reverse,
                    &self.bias_ih_reverse,
                    &self.bias_hh_reverse,
                );
                reversed.push(next_h.clone());
                prev_h = next_h;
                prev_c = next_c;
            }
            reversed.reverse();
            Ok(reversed)
        }

        fn score_target(
            &self,
            fw_h: &Array1<f32>,
            bw_h: &Array1<f32>,
            candidates: &[String],
        ) -> Result<ModelDecision, String> {
            let hidden = fw_h.len() + bw_h.len();
            let mut target_hidden = Array1::<f32>::zeros(hidden);
            for (idx, value) in fw_h.iter().enumerate() {
                target_hidden[idx] = *value;
            }
            for (idx, value) in bw_h.iter().enumerate() {
                target_hidden[fw_h.len() + idx] = *value;
            }

            let hidden0 = self.hidden_weight_l0.dot(&target_hidden) + &self.hidden_bias_l0;
            let hidden0 = hidden0.mapv(Self::relu);
            let logits = self.hidden_weight_l1.dot(&hidden0) + &self.hidden_bias_l1;

            let mut scored = Vec::new();
            for candidate in candidates {
                let Some(indices) = self.label_buckets.get(candidate) else {
                    continue;
                };
                let mut best = f32::NEG_INFINITY;
                for idx in indices {
                    if *idx < logits.len() {
                        best = best.max(logits[*idx]);
                    }
                }
                if best.is_finite() {
                    scored.push((candidate.clone(), best));
                }
            }

            if scored.is_empty() {
                return Ok(ModelDecision {
                    chosen: candidates.first().cloned().unwrap_or_default(),
                    confidence: 0.0,
                    margin: 0.0,
                });
            }

            scored.sort_by(|a, b| b.1.total_cmp(&a.1));
            let max_logit = scored[0].1;
            let exp_sum: f32 = scored
                .iter()
                .map(|(_, logit)| (*logit - max_logit).exp())
                .sum();
            let top_prob = if exp_sum > 0.0 {
                (scored[0].1 - max_logit).exp() / exp_sum
            } else {
                0.0
            };
            let second_prob = if scored.len() >= 2 && exp_sum > 0.0 {
                (scored[1].1 - max_logit).exp() / exp_sum
            } else {
                0.0
            };

            Ok(ModelDecision {
                chosen: scored[0].0.clone(),
                confidence: top_prob,
                margin: top_prob - second_prob,
            })
        }

        fn load(spec: &ModelSpec) -> Result<Self, String> {
            let manifest_path = Self::resolve_manifest_path(&spec.model_path)?;
            let manifest = Self::read_manifest(&manifest_path)?;
            let char2idx = Self::parse_char2idx(&manifest, &manifest_path)?;
            let idx2class = Self::parse_idx2class(&manifest, &manifest_path)?;
            let normalized_labels = idx2class
                .iter()
                .map(|label| normalize_model_pinyin(label))
                .collect::<Vec<_>>();
            let mut label_buckets: HashMap<String, Vec<usize>> = HashMap::new();
            for (idx, label) in normalized_labels.iter().enumerate() {
                label_buckets.entry(label.clone()).or_default().push(idx);
            }

            let embeddings = Self::load_array2(&manifest, &manifest_path, "embedding_weight")?;
            let weight_ih = Self::load_array2(&manifest, &manifest_path, "weight_ih")?;
            let weight_hh = Self::load_array2(&manifest, &manifest_path, "weight_hh")?;
            let bias_ih = Self::load_array1(&manifest, &manifest_path, "bias_ih")?;
            let bias_hh = Self::load_array1(&manifest, &manifest_path, "bias_hh")?;
            let weight_ih_reverse =
                Self::load_array2(&manifest, &manifest_path, "weight_ih_reverse")?;
            let weight_hh_reverse =
                Self::load_array2(&manifest, &manifest_path, "weight_hh_reverse")?;
            let bias_ih_reverse = Self::load_array1(&manifest, &manifest_path, "bias_ih_reverse")?;
            let bias_hh_reverse = Self::load_array1(&manifest, &manifest_path, "bias_hh_reverse")?;
            let hidden_weight_l0 =
                Self::load_array2(&manifest, &manifest_path, "hidden_weight_l0")?;
            let hidden_bias_l0 = Self::load_array1(&manifest, &manifest_path, "hidden_bias_l0")?;
            let hidden_weight_l1 =
                Self::load_array2(&manifest, &manifest_path, "hidden_weight_l1")?;
            let hidden_bias_l1 = Self::load_array1(&manifest, &manifest_path, "hidden_bias_l1")?;

            let unk_id = *char2idx
                .get("<UNK>")
                .ok_or_else(|| "g2pm manifest char2idx is missing '<UNK>'".to_string())?;
            let bos_id = *char2idx
                .get("시")
                .ok_or_else(|| "g2pm manifest char2idx is missing BOS token '시'".to_string())?;
            let eos_id = *char2idx
                .get("끝")
                .ok_or_else(|| "g2pm manifest char2idx is missing EOS token '끝'".to_string())?;

            Ok(Self {
                char2idx,
                label_buckets,
                embeddings,
                weight_ih,
                weight_hh,
                bias_ih,
                bias_hh,
                weight_ih_reverse,
                weight_hh_reverse,
                bias_ih_reverse,
                bias_hh_reverse,
                hidden_weight_l0,
                hidden_bias_l0,
                hidden_weight_l1,
                hidden_bias_l1,
                unk_id,
                bos_id,
                eos_id,
            })
        }
    }

    #[cfg(feature = "g2pm")]
    impl PolyphoneModel for G2pmNumpyModel {
        fn disambiguate(&self, req: &ModelRequest) -> Result<Vec<ModelDecision>, String> {
            if req.target_char_offsets.len() != req.candidate_sets.len() {
                return Err(
                    "model request candidate count does not match target offsets".to_string(),
                );
            }

            let chars: Vec<char> = req.sentence.chars().collect();
            let input_ids = self.sentence_input_ids(&req.sentence);
            let fw_states = self.forward_hidden_states(&input_ids)?;
            let bw_states = self.backward_hidden_states(&input_ids)?;

            let mut out = Vec::with_capacity(req.target_char_offsets.len());
            for (offset, candidates) in req
                .target_char_offsets
                .iter()
                .zip(req.candidate_sets.iter())
            {
                let Some(_query_char) = chars.get(*offset) else {
                    return Err(format!(
                        "query offset {} is out of range for sentence length {}",
                        offset,
                        chars.len()
                    ));
                };
                let state_idx = offset + 1;
                let Some(fw_h) = fw_states.get(state_idx) else {
                    return Err(format!(
                        "g2pm forward state index {} is out of range for sentence '{}'",
                        state_idx, req.sentence
                    ));
                };
                let Some(bw_h) = bw_states.get(state_idx) else {
                    return Err(format!(
                        "g2pm backward state index {} is out of range for sentence '{}'",
                        state_idx, req.sentence
                    ));
                };
                out.push(self.score_target(fw_h, bw_h, candidates)?);
            }

            Ok(out)
        }
    }

    #[derive(Clone)]
    struct ModelRuntime {
        config: ModelConfig,
        backend: Arc<dyn PolyphoneModel>,
    }

    #[derive(Clone, Default)]
    struct ModelState {
        version: i64,
        loaded: bool,
        active_model: Option<String>,
        runtime: Option<Arc<ModelRuntime>>,
        failed_message: Option<String>,
        status: String,
        config: ModelConfig,
        guc_signature: Option<String>,
    }

    static CHAR_DICTIONARY_CACHE: OnceLock<RwLock<CharDictionaryCache>> = OnceLock::new();
    static DICTIONARY_CACHE: OnceLock<RwLock<DictionaryCache>> = OnceLock::new();
    static TOKEN_DICTIONARY_CACHE: OnceLock<RwLock<TokenDictionaryCache>> = OnceLock::new();
    static SUFFIX_DICTIONARY_CACHE: OnceLock<RwLock<HashMap<String, SuffixDictionaryCacheEntry>>> =
        OnceLock::new();
    static MODEL_CACHE: OnceLock<RwLock<HashMap<String, ModelState>>> = OnceLock::new();

    #[pg_guard]
    pub extern "C-unwind" fn _PG_init() {
        GucRegistry::define_int_guc(
            c"pg_pinyin.g2pw_window_size",
            c"Override g2pW text window size for ONNX feature construction",
            c"Set to 0 to use the model registry/default value.",
            &G2PW_WINDOW_SIZE_GUC,
            0,
            512,
            GucContext::Userset,
            GucFlags::default(),
        );
        GucRegistry::define_int_guc(
            c"pg_pinyin.g2pw_intra_op_num_threads",
            c"Override g2pW ONNX Runtime intra-op thread count",
            c"Set to 0 to use the model registry/default value.",
            &G2PW_INTRA_THREADS_GUC,
            0,
            64,
            GucContext::Userset,
            GucFlags::default(),
        );
        GucRegistry::define_float_guc(
            c"pg_pinyin.model_min_confidence",
            c"Override the minimum confidence required to accept a model prediction",
            c"Set to a negative value to use the model registry/default value.",
            &MODEL_MIN_CONFIDENCE_GUC,
            -1.0,
            1.0,
            GucContext::Userset,
            GucFlags::default(),
        );
        GucRegistry::define_float_guc(
            c"pg_pinyin.model_min_margin",
            c"Override the minimum margin required to accept a model prediction",
            c"Set to a negative value to use the model registry/default value.",
            &MODEL_MIN_MARGIN_GUC,
            -1.0,
            1.0,
            GucContext::Userset,
            GucFlags::default(),
        );
    }

    fn char_dictionary_cache() -> &'static RwLock<CharDictionaryCache> {
        CHAR_DICTIONARY_CACHE.get_or_init(|| RwLock::new(CharDictionaryCache::default()))
    }

    fn dictionary_cache() -> &'static RwLock<DictionaryCache> {
        DICTIONARY_CACHE.get_or_init(|| RwLock::new(DictionaryCache::default()))
    }

    fn token_dictionary_cache() -> &'static RwLock<TokenDictionaryCache> {
        TOKEN_DICTIONARY_CACHE.get_or_init(|| RwLock::new(TokenDictionaryCache::default()))
    }

    fn suffix_dictionary_cache() -> &'static RwLock<HashMap<String, SuffixDictionaryCacheEntry>> {
        SUFFIX_DICTIONARY_CACHE.get_or_init(|| RwLock::new(HashMap::new()))
    }

    fn model_cache() -> &'static RwLock<HashMap<String, ModelState>> {
        MODEL_CACHE.get_or_init(|| RwLock::new(HashMap::new()))
    }

    fn sql_literal(value: &str) -> String {
        format!("'{}'", value.replace('\'', "''"))
    }

    fn fetch_string_map_from_table(
        table: &str,
        key_col: &str,
        value_col: &str,
    ) -> HashMap<String, String> {
        let query = format!(
            "SELECT {key_col}, {value_col} FROM {schema}.{table}",
            key_col = key_col,
            value_col = value_col,
            schema = DICTIONARY_SCHEMA,
            table = table,
        );

        Spi::connect(|client| {
            let rows = match client.select(&query, None, &[]) {
                Ok(rows) => rows,
                Err(err) => error!("SPI query failed: {err}. query={query}"),
            };

            let mut out = HashMap::with_capacity(rows.len());
            for row in rows {
                let key = match row[key_col].value::<String>() {
                    Ok(Some(v)) => v,
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for {table}.{key_col}: {err}"),
                };
                let value = match row[value_col].value::<String>() {
                    Ok(Some(v)) => v,
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for {table}.{value_col}: {err}"),
                };
                out.insert(key, value);
            }

            out
        })
    }

    fn fetch_regex_tokens_from_table() -> Vec<String> {
        let query = format!(
            "SELECT character
             FROM {schema}.pinyin_token
             WHERE category = 1 OR character IN ('zh', 'ch', 'sh')",
            schema = DICTIONARY_SCHEMA,
        );

        Spi::connect(|client| {
            let rows = match client.select(&query, None, &[]) {
                Ok(rows) => rows,
                Err(err) => error!("SPI query failed: {err}. query={query}"),
            };

            let mut out = Vec::with_capacity(rows.len() + 3);
            for row in rows {
                let token = match row["character"].value::<String>() {
                    Ok(Some(v)) => v.to_ascii_lowercase(),
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for pinyin_token.character: {err}"),
                };
                out.push(token);
            }
            out
        })
    }

    fn fetch_overlayed_string_map(
        base_table: &str,
        key_col: &str,
        value_col: &str,
        overlay_table: Option<&str>,
    ) -> HashMap<String, String> {
        let mut out = fetch_string_map_from_table(base_table, key_col, value_col);
        if let Some(overlay) = overlay_table {
            out.extend(fetch_string_map_from_table(overlay, key_col, value_col));
        }
        out
    }

    fn parse_embedded_string_rows(csv_text: &str, csv_name: &str) -> Vec<(String, String)> {
        let mut rows = Vec::new();
        for (idx, line) in csv_text.lines().enumerate() {
            let row = line.trim_end_matches('\r');
            if row.is_empty() {
                continue;
            }
            let Some((first, second)) = row.split_once(',') else {
                error!(
                    "failed parsing embedded {csv_name} CSV at line {}: invalid two-column format",
                    idx + 1
                );
            };
            rows.push((first.to_string(), second.to_string()));
        }
        rows
    }

    fn parse_embedded_token_rows(csv_text: &str) -> Vec<(String, i16)> {
        let mut rows = Vec::new();
        for (idx, line) in csv_text.lines().enumerate() {
            let row = line.trim_end_matches('\r');
            if row.is_empty() {
                continue;
            }
            let Some((token, category_text)) = row.split_once(',') else {
                error!(
                    "failed parsing embedded token CSV at line {}: invalid two-column format",
                    idx + 1
                );
            };
            let category: i16 = match category_text.parse() {
                Ok(v) => v,
                Err(_) => error!("invalid token category in embedded CSV: {category_text}"),
            };
            rows.push((token.to_string(), category));
        }
        rows
    }

    fn bulk_insert_string_rows(
        table: &str,
        key_col: &str,
        value_col: &str,
        rows: &[(String, String)],
    ) {
        for chunk in rows.chunks(400) {
            let mut values = String::new();
            for (idx, (key, value)) in chunk.iter().enumerate() {
                if idx > 0 {
                    values.push(',');
                }
                values.push_str(&format!("({}, {})", sql_literal(key), sql_literal(value)));
            }
            let sql = format!(
                "INSERT INTO {table} ({key_col}, {value_col}) VALUES {values} \
                 ON CONFLICT ({key_col}) DO UPDATE SET {value_col} = EXCLUDED.{value_col}"
            );
            if let Err(err) = Spi::run(&sql) {
                error!("failed bulk insert into {table}: {err}");
            }
        }
    }

    fn bulk_insert_token_rows(table: &str, rows: &[(String, i16)]) {
        for chunk in rows.chunks(400) {
            let mut values = String::new();
            for (idx, (token, category)) in chunk.iter().enumerate() {
                if idx > 0 {
                    values.push(',');
                }
                values.push_str(&format!("({}, {})", sql_literal(token), category));
            }
            let sql = format!(
                "INSERT INTO {table} (character, category) VALUES {values} \
                 ON CONFLICT (character) DO UPDATE SET category = EXCLUDED.category"
            );
            if let Err(err) = Spi::run(&sql) {
                error!("failed bulk insert into {table}: {err}");
            }
        }
    }

    fn write_temp_csv(prefix: &str, csv_text: &str) -> String {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = format!("/tmp/pg_pinyin_{prefix}_{}_{}.csv", process::id(), now);
        if let Err(err) = fs::write(&path, csv_text) {
            error!("failed writing temp CSV file {path}: {err}");
        }
        path
    }

    fn try_copy_csv_to_table(
        table: &str,
        columns: &str,
        prefix: &str,
        csv_text: &str,
    ) -> Result<(), String> {
        let path = write_temp_csv(prefix, csv_text);
        let sql = format!(
            "COPY {table} ({columns}) FROM {} WITH (FORMAT csv, HEADER false)",
            sql_literal(&path)
        );
        let result = Spi::run(&sql).map_err(|err| err.to_string());
        let _ = fs::remove_file(&path);
        result
    }

    fn seed_embedded_dictionary_data() {
        let truncate_sql = format!(
            "TRUNCATE TABLE {s}.pinyin_mapping; \
             TRUNCATE TABLE {s}.pinyin_token; \
             TRUNCATE TABLE {s}.pinyin_words;",
            s = DICTIONARY_SCHEMA
        );
        if let Err(err) = Spi::run(&truncate_sql) {
            error!("failed truncating dictionary tables before seed: {err}");
        }

        let copy_attempt = try_copy_csv_to_table(
            &format!("{DICTIONARY_SCHEMA}.pinyin_mapping"),
            "character, pinyin",
            "mapping",
            EMBEDDED_MAPPING_CSV,
        )
        .and_then(|_| {
            try_copy_csv_to_table(
                &format!("{DICTIONARY_SCHEMA}.pinyin_token"),
                "character, category",
                "token",
                EMBEDDED_TOKEN_CSV,
            )
        })
        .and_then(|_| {
            try_copy_csv_to_table(
                &format!("{DICTIONARY_SCHEMA}.pinyin_words"),
                "word, pinyin",
                "words",
                EMBEDDED_WORDS_CSV,
            )
        });

        if let Err(copy_err) = copy_attempt {
            let mapping_rows = parse_embedded_string_rows(EMBEDDED_MAPPING_CSV, "mapping");
            let token_rows = parse_embedded_token_rows(EMBEDDED_TOKEN_CSV);
            let word_rows = parse_embedded_string_rows(EMBEDDED_WORDS_CSV, "word");

            if let Err(err) = Spi::run(&truncate_sql) {
                error!("failed truncating dictionary tables for fallback insert: {err}");
            }

            bulk_insert_string_rows(
                &format!("{DICTIONARY_SCHEMA}.pinyin_mapping"),
                "character",
                "pinyin",
                &mapping_rows,
            );
            bulk_insert_token_rows(&format!("{DICTIONARY_SCHEMA}.pinyin_token"), &token_rows);
            bulk_insert_string_rows(
                &format!("{DICTIONARY_SCHEMA}.pinyin_words"),
                "word",
                "pinyin",
                &word_rows,
            );

            warning!("COPY-based dictionary seed failed, fallback to INSERT: {copy_err}");
        }

        let space_sql = format!(
            "INSERT INTO {s}.pinyin_mapping (character, pinyin) VALUES (' ', ' ') \
             ON CONFLICT (character) DO NOTHING",
            s = DICTIONARY_SCHEMA
        );
        if let Err(err) = Spi::run(&space_sql) {
            error!("failed ensuring space mapping row: {err}");
        }
    }

    fn fetch_dictionary_version() -> i64 {
        let sql = format!(
            "SELECT COALESCE((SELECT version FROM {s}.pinyin_dictionary_meta WHERE singleton), 0)",
            s = DICTIONARY_SCHEMA
        );
        match Spi::get_one::<i64>(&sql) {
            Ok(Some(version)) => version,
            Ok(None) => 0,
            Err(_) => 0,
        }
    }

    fn fetch_model_version() -> i64 {
        let sql = format!(
            "SELECT COALESCE((SELECT version FROM {s}.pinyin_model_meta WHERE singleton), 0)",
            s = DICTIONARY_SCHEMA
        );
        match Spi::get_one::<i64>(&sql) {
            Ok(Some(version)) => version,
            Ok(None) => 0,
            Err(_) => 0,
        }
    }

    fn load_char_dictionary_snapshot(version: i64) -> CharDictionaryCache {
        let char_map = fetch_string_map_from_table("pinyin_mapping", "character", "pinyin");
        CharDictionaryCache {
            version,
            loaded: true,
            char_map,
        }
    }

    fn with_char_dictionary_cache<R>(f: impl FnOnce(&HashMap<String, String>) -> R) -> R {
        let version = fetch_dictionary_version();
        let lock = char_dictionary_cache();

        {
            let cache = lock
                .read()
                .expect("char dictionary cache read lock poisoned");
            if cache.loaded && cache.version == version {
                return f(&cache.char_map);
            }
        }

        let snapshot = load_char_dictionary_snapshot(version);

        {
            let mut cache = lock
                .write()
                .expect("char dictionary cache write lock poisoned");
            if !cache.loaded || cache.version != version {
                *cache = snapshot;
            }
            f(&cache.char_map)
        }
    }

    fn load_dictionary_snapshot(version: i64) -> DictionaryCache {
        let char_map = fetch_string_map_from_table("pinyin_mapping", "character", "pinyin");
        let word_map = fetch_string_map_from_table("pinyin_words", "word", "pinyin");

        let max_word_len = word_map
            .keys()
            .map(|word| word.chars().count())
            .max()
            .unwrap_or(0);

        DictionaryCache {
            version,
            loaded: true,
            char_map,
            word_map,
            max_word_len,
        }
    }

    fn with_dictionary_cache<R>(f: impl FnOnce(&DictionaryCache) -> R) -> R {
        let version = fetch_dictionary_version();
        let lock = dictionary_cache();

        {
            let cache = lock.read().expect("dictionary cache read lock poisoned");
            if cache.loaded && cache.version == version {
                return f(&cache);
            }
        }

        let snapshot = load_dictionary_snapshot(version);

        {
            let mut cache = lock.write().expect("dictionary cache write lock poisoned");
            if !cache.loaded || cache.version != version {
                *cache = snapshot;
            }
            f(&cache)
        }
    }

    fn load_token_dictionary_snapshot(version: i64) -> TokenDictionaryCache {
        TokenDictionaryCache {
            version,
            loaded: true,
            regex_tokens: Some(RegexTokenDictionary::from_tokens(
                fetch_regex_tokens_from_table(),
            )),
        }
    }

    fn with_token_dictionary_cache<R>(f: impl FnOnce(&RegexTokenDictionary) -> R) -> R {
        let version = fetch_dictionary_version();
        let lock = token_dictionary_cache();

        {
            let cache = lock.read().expect("token dictionary cache read lock poisoned");
            if cache.loaded && cache.version == version {
                return f(cache.regex_tokens.as_ref().expect("token cache missing"));
            }
        }

        let snapshot = load_token_dictionary_snapshot(version);

        {
            let mut cache = lock
                .write()
                .expect("token dictionary cache write lock poisoned");
            if !cache.loaded || cache.version != version {
                *cache = snapshot;
            }
            f(cache.regex_tokens.as_ref().expect("token cache missing"))
        }
    }

    fn canonicalize_table_suffix(suffix: &str) -> Option<String> {
        let trimmed = suffix.trim();
        if trimmed.is_empty() {
            return None;
        }

        let normalized = trimmed.trim_start_matches('_');
        if normalized.is_empty() {
            error!("dictionary table suffix cannot be empty");
        }

        if !normalized
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
        {
            error!("dictionary table suffix must contain only [A-Za-z0-9_]");
        }

        Some(format!("_{}", normalized.to_ascii_lowercase()))
    }

    fn table_exists(schema: &str, table: &str) -> bool {
        let query = format!(
            "SELECT EXISTS (
               SELECT 1
               FROM pg_catalog.pg_class AS c
               JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
               WHERE n.nspname = {schema}
                 AND c.relname = {table}
                 AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
             )",
            schema = sql_literal(schema),
            table = sql_literal(table),
        );
        match Spi::get_one::<bool>(&query) {
            Ok(Some(v)) => v,
            _ => false,
        }
    }

    fn overlay_table_name(base_name: &str, canonical_suffix: Option<&str>) -> Option<String> {
        canonical_suffix
            .map(|s| format!("{base_name}{s}"))
            .filter(|table| table_exists(DICTIONARY_SCHEMA, table))
    }

    fn load_char_map_from_canonical_suffix(
        canonical_suffix: Option<&str>,
    ) -> HashMap<String, String> {
        let overlay_mapping = overlay_table_name("pinyin_mapping", canonical_suffix);
        fetch_overlayed_string_map(
            "pinyin_mapping",
            "character",
            "pinyin",
            overlay_mapping.as_deref(),
        )
    }

    fn load_overlay_char_map_from_canonical_suffix(
        canonical_suffix: Option<&str>,
    ) -> HashMap<String, String> {
        match overlay_table_name("pinyin_mapping", canonical_suffix) {
            Some(overlay) => fetch_string_map_from_table(&overlay, "character", "pinyin"),
            None => HashMap::new(),
        }
    }

    fn load_word_map_from_canonical_suffix(
        canonical_suffix: Option<&str>,
    ) -> (HashMap<String, String>, usize) {
        let overlay_words = overlay_table_name("pinyin_words", canonical_suffix);
        let word_map =
            fetch_overlayed_string_map("pinyin_words", "word", "pinyin", overlay_words.as_deref());
        let max_word_len = word_map
            .keys()
            .map(|word| word.chars().count())
            .max()
            .unwrap_or(0);
        (word_map, max_word_len)
    }

    fn load_overlay_word_map_from_canonical_suffix(
        canonical_suffix: Option<&str>,
    ) -> HashMap<String, String> {
        match overlay_table_name("pinyin_words", canonical_suffix) {
            Some(overlay) => fetch_string_map_from_table(&overlay, "word", "pinyin"),
            None => HashMap::new(),
        }
    }

    fn clear_all_suffix_cache_impl() -> i64 {
        let lock = suffix_dictionary_cache();
        let mut cache = lock
            .write()
            .expect("suffix dictionary cache write lock poisoned");
        let cleared = cache.len() as i64;
        cache.clear();
        cleared
    }

    fn clear_suffix_cache_impl(suffix: &str) -> bool {
        let canonical_suffix = match canonicalize_table_suffix(suffix) {
            Some(value) => value,
            None => return false,
        };

        let lock = suffix_dictionary_cache();
        let mut cache = lock
            .write()
            .expect("suffix dictionary cache write lock poisoned");
        cache.remove(&canonical_suffix).is_some()
    }

    fn with_suffix_char_cache<R>(
        canonical_suffix: &str,
        f: impl FnOnce(&SuffixDictionaryCacheEntry) -> R,
    ) -> R {
        let base_version = fetch_dictionary_version();
        let lock = suffix_dictionary_cache();

        {
            let cache = lock
                .read()
                .expect("suffix dictionary cache read lock poisoned");
            if let Some(entry) = cache.get(canonical_suffix) {
                if entry.base_version == base_version {
                    return f(entry);
                }
            }
        }

        let char_map = load_char_map_from_canonical_suffix(Some(canonical_suffix));
        let overlay_char_map = load_overlay_char_map_from_canonical_suffix(Some(canonical_suffix));

        {
            let mut cache = lock
                .write()
                .expect("suffix dictionary cache write lock poisoned");
            let entry = cache.entry(canonical_suffix.to_string()).or_default();
            if entry.base_version != base_version {
                *entry = SuffixDictionaryCacheEntry {
                    base_version,
                    char_map,
                    overlay_char_map,
                    words_loaded: false,
                    word_map: HashMap::new(),
                    overlay_word_map: HashMap::new(),
                    max_word_len: 0,
                };
            }
            f(entry)
        }
    }

    fn with_suffix_word_cache<R>(
        canonical_suffix: &str,
        f: impl FnOnce(&SuffixDictionaryCacheEntry) -> R,
    ) -> R {
        let base_version = fetch_dictionary_version();
        let lock = suffix_dictionary_cache();

        {
            let cache = lock
                .read()
                .expect("suffix dictionary cache read lock poisoned");
            if let Some(entry) = cache.get(canonical_suffix) {
                if entry.base_version == base_version && entry.words_loaded {
                    return f(entry);
                }
            }
        }

        let char_map = load_char_map_from_canonical_suffix(Some(canonical_suffix));
        let overlay_char_map = load_overlay_char_map_from_canonical_suffix(Some(canonical_suffix));
        let (word_map, max_word_len) = load_word_map_from_canonical_suffix(Some(canonical_suffix));
        let overlay_word_map = load_overlay_word_map_from_canonical_suffix(Some(canonical_suffix));

        {
            let mut cache = lock
                .write()
                .expect("suffix dictionary cache write lock poisoned");
            let entry = cache.entry(canonical_suffix.to_string()).or_default();
            if entry.base_version != base_version || !entry.words_loaded {
                *entry = SuffixDictionaryCacheEntry {
                    base_version,
                    char_map,
                    overlay_char_map,
                    words_loaded: true,
                    word_map,
                    overlay_word_map,
                    max_word_len,
                };
            }
            f(entry)
        }
    }

    fn parse_candidates(raw: &str) -> Vec<String> {
        let mut out = Vec::new();
        for part in raw.split('|') {
            let candidate = part.trim().to_ascii_lowercase();
            if !candidate.is_empty() && !out.iter().any(|existing| existing == &candidate) {
                out.push(candidate);
            }
        }

        if out.is_empty() {
            let candidate = raw.trim().to_ascii_lowercase();
            if !candidate.is_empty() {
                out.push(candidate);
            }
        }

        out
    }

    fn romanize_first_candidate(raw: &str) -> String {
        parse_candidates(raw)
            .into_iter()
            .next()
            .unwrap_or_else(|| raw.to_ascii_lowercase())
    }

    fn romanize_pinyin_phrase(raw: &str) -> String {
        let mut out = Vec::new();
        for part in raw.split_whitespace() {
            let token = romanize_first_candidate(part);
            if !token.is_empty() {
                out.push(token);
            }
        }

        if out.is_empty() {
            romanize_first_candidate(raw)
        } else {
            out.join(" ")
        }
    }

    fn normalize_model_pinyin(raw: &str) -> String {
        raw.trim()
            .trim_matches('|')
            .trim_end_matches(|ch: char| ch.is_ascii_digit())
            .to_ascii_lowercase()
    }

    fn parse_model_config(value: &Value) -> ModelConfig {
        let mut config = ModelConfig::default();
        if let Some(raw) = value.get("min_confidence").and_then(Value::as_f64) {
            config.min_confidence = raw as f32;
        }
        if let Some(raw) = value.get("min_margin").and_then(Value::as_f64) {
            config.min_margin = raw as f32;
        }
        if let Some(raw) = value.get("disable_on_error").and_then(Value::as_bool) {
            config.disable_on_error = raw;
        }
        config
    }

    fn apply_model_config_guc_overrides(mut config: ModelConfig) -> ModelConfig {
        let min_confidence = MODEL_MIN_CONFIDENCE_GUC.get();
        if min_confidence >= 0.0 {
            config.min_confidence = min_confidence as f32;
        }
        let min_margin = MODEL_MIN_MARGIN_GUC.get();
        if min_margin >= 0.0 {
            config.min_margin = min_margin as f32;
        }
        config
    }

    #[cfg(feature = "hybrid_onnx")]
    fn parse_g2pw_load_config(value: &Value) -> G2pwLoadConfig {
        let window_size = value
            .get("window_size")
            .and_then(Value::as_u64)
            .map(|value| value as usize)
            .unwrap_or(DEFAULT_G2PW_WINDOW_SIZE);
        let intra_op_num_threads = value
            .get("intra_op_num_threads")
            .and_then(Value::as_u64)
            .map(|value| value as usize)
            .unwrap_or(DEFAULT_G2PW_INTRA_THREADS);

        G2pwLoadConfig {
            window_size,
            intra_op_num_threads,
        }
    }

    #[cfg(feature = "hybrid_onnx")]
    fn apply_g2pw_load_guc_overrides(mut config: G2pwLoadConfig) -> G2pwLoadConfig {
        let window_size = G2PW_WINDOW_SIZE_GUC.get();
        if window_size > 0 {
            config.window_size = window_size as usize;
        }
        let intra_threads = G2PW_INTRA_THREADS_GUC.get();
        if intra_threads > 0 {
            config.intra_op_num_threads = intra_threads as usize;
        }
        config
    }

    fn current_model_runtime_guc_signature() -> String {
        format!(
            "g2pw_window_size={};g2pw_intra_threads={}",
            G2PW_WINDOW_SIZE_GUC.get(),
            G2PW_INTRA_THREADS_GUC.get()
        )
    }

    fn parse_mock_decisions(value: &Value) -> HashMap<String, MockDecision> {
        let mut out = HashMap::new();
        let Some(obj) = value.get("mock_char_decisions").and_then(Value::as_object) else {
            return out;
        };

        for (ch, raw_decision) in obj {
            let Some(decision_obj) = raw_decision.as_object() else {
                continue;
            };
            let Some(chosen) = decision_obj
                .get("chosen")
                .and_then(Value::as_str)
                .map(normalize_model_pinyin)
            else {
                continue;
            };
            if chosen.is_empty() {
                continue;
            }

            let confidence = decision_obj
                .get("confidence")
                .and_then(Value::as_f64)
                .unwrap_or(DEFAULT_MIN_CONFIDENCE as f64) as f32;
            let margin = decision_obj
                .get("margin")
                .and_then(Value::as_f64)
                .unwrap_or(DEFAULT_MIN_MARGIN as f64) as f32;
            out.insert(
                ch.clone(),
                MockDecision {
                    chosen,
                    confidence,
                    margin,
                },
            );
        }

        out
    }

    fn normalize_model_identifier(model: &str) -> Option<String> {
        let normalized = model.trim().to_ascii_lowercase();
        if normalized.is_empty() {
            return None;
        }

        Some(match normalized.as_str() {
            "g2pw" => "g2pw_onnx".to_string(),
            "g2pm" => "g2pm_numpy".to_string(),
            other => other.to_string(),
        })
    }

    fn fetch_model_specs_by_kind(kind: &str) -> Result<Vec<ModelSpec>, String> {
        let sql = format!(
            "SELECT jsonb_build_object(
               'model_name', model_name,
               'kind', kind,
               'model_path', model_path,
               'tokenizer_path', tokenizer_path,
               'labels_path', labels_path,
               'config', config
             )::text
             FROM {s}.pinyin_model_registry
             WHERE enabled
               AND lower(kind) = {kind}
             ORDER BY model_name",
            s = DICTIONARY_SCHEMA,
            kind = sql_literal(kind),
        );

        let payloads = Spi::connect(|client| {
            let rows = client.select(&sql, None, &[])?;
            let mut payloads = Vec::with_capacity(rows.len());

            for row in rows {
                match row["jsonb_build_object"].value::<String>() {
                    Ok(Some(payload)) => payloads.push(payload),
                    Ok(None) => {}
                    Err(err) => {
                        error!("failed parsing model registry row for kind '{kind}': {err}")
                    }
                }
            }

            Ok::<Vec<String>, pgrx::spi::SpiError>(payloads)
        })
        .map_err(|err| err.to_string())?;
        let mut out = Vec::with_capacity(payloads.len());

        for payload in payloads {
            let value: Value = serde_json::from_str(&payload)
                .map_err(|err| format!("invalid model spec JSON: {err}"))?;
            out.push(ModelSpec {
                model_name: value
                    .get("model_name")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                kind: value
                    .get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                #[cfg(feature = "g2pm")]
                model_path: value
                    .get("model_path")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                #[cfg(feature = "hybrid_onnx")]
                tokenizer_path: value
                    .get("tokenizer_path")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                #[cfg(feature = "hybrid_onnx")]
                labels_path: value
                    .get("labels_path")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                config: value.get("config").cloned().unwrap_or_else(|| json!({})),
            });
        }

        Ok(out)
    }

    fn fetch_model_spec_by_identifier(model: &str) -> Result<ModelSpec, String> {
        let Some(kind) = normalize_model_identifier(model) else {
            return Err("model identifier cannot be empty".to_string());
        };
        let specs = fetch_model_specs_by_kind(&kind)?;
        match specs.len() {
            0 => Err(format!(
                "no enabled model found for model => '{}' (resolved kind '{}')",
                model, kind
            )),
            1 => Ok(specs.into_iter().next().expect("single spec must exist")),
            _ => {
                let names = specs
                    .iter()
                    .map(|spec| spec.model_name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ");
                Err(format!(
                    "multiple enabled models found for model => '{}' (resolved kind '{}'): {}. Keep exactly one enabled row for that kind",
                    model, kind, names
                ))
            }
        }
    }

    fn load_model_runtime(spec: &ModelSpec) -> Result<ModelRuntime, String> {
        let config = apply_model_config_guc_overrides(parse_model_config(&spec.config));
        let mock_decisions = parse_mock_decisions(&spec.config);

        let backend: Arc<dyn PolyphoneModel> = if !mock_decisions.is_empty() {
            Arc::new(ConfigOnlyModel {
                decisions: mock_decisions,
            })
        } else {
            match spec.kind.as_str() {
                "g2pw_onnx" => {
                    #[cfg(feature = "hybrid_onnx")]
                    {
                        let g2pw_load_config =
                            apply_g2pw_load_guc_overrides(parse_g2pw_load_config(&spec.config));
                        Arc::new(G2pwOnnxModel::load(spec, &g2pw_load_config)?)
                    }
                    #[cfg(not(feature = "hybrid_onnx"))]
                    {
                        return Err(
                            "active model requires feature 'hybrid_onnx'; rebuild the extension with that feature enabled"
                                .to_string(),
                        );
                    }
                }
                "g2pm_numpy" => {
                    #[cfg(feature = "g2pm")]
                    {
                        Arc::new(G2pmNumpyModel::load(spec)?)
                    }
                    #[cfg(not(feature = "g2pm"))]
                    {
                        return Err(
                            "active model requires feature 'g2pm'; rebuild the extension with that feature enabled"
                                .to_string(),
                        );
                    }
                }
                "small_onnx" => {
                    return Err("kind 'small_onnx' is reserved but not implemented yet".to_string());
                }
                other => return Err(format!("unsupported model kind '{other}'")),
            }
        };

        Ok(ModelRuntime { config, backend })
    }

    fn disabled_model_state() -> ModelState {
        ModelState {
            version: fetch_model_version(),
            loaded: true,
            active_model: None,
            runtime: None,
            failed_message: None,
            status: "disabled".to_string(),
            config: ModelConfig::default(),
            guc_signature: None,
        }
    }

    fn load_model_state(version: i64, requested_kind: &str) -> Result<ModelState, String> {
        let spec = fetch_model_spec_by_identifier(requested_kind)?;

        let config = apply_model_config_guc_overrides(parse_model_config(&spec.config));
        let guc_signature = Some(current_model_runtime_guc_signature());
        Ok(match load_model_runtime(&spec) {
            Ok(runtime) => ModelState {
                version,
                loaded: true,
                active_model: Some(spec.model_name.clone()),
                runtime: Some(Arc::new(runtime)),
                failed_message: None,
                status: "ready".to_string(),
                config,
                guc_signature,
            },
            Err(err) => ModelState {
                version,
                loaded: true,
                active_model: Some(spec.model_name.clone()),
                runtime: None,
                failed_message: Some(err),
                status: "failed".to_string(),
                config,
                guc_signature,
            },
        })
    }

    fn get_model_state_for_identifier(model: Option<&str>) -> Result<ModelState, String> {
        let Some(model) = model.and_then(normalize_model_identifier) else {
            return Ok(disabled_model_state());
        };

        let version = fetch_model_version();
        let guc_signature = current_model_runtime_guc_signature();
        let lock = model_cache();

        {
            let cache = lock.read().expect("model cache read lock poisoned");
            if let Some(state) = cache.get(&model) {
                if state.loaded
                    && state.version == version
                    && state.guc_signature.as_deref() == Some(guc_signature.as_str())
                {
                    let mut state = state.clone();
                    state.config = apply_model_config_guc_overrides(state.config);
                    return Ok(state);
                }
            }
        }

        let snapshot = load_model_state(version, &model)?;

        {
            let mut cache = lock.write().expect("model cache write lock poisoned");
            cache.insert(model.clone(), snapshot.clone());
        }

        Ok(snapshot)
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum PieceKind {
        AsciiRun,
        Space,
        Other,
    }

    #[derive(Clone, Debug)]
    struct Piece {
        value: String,
        kind: PieceKind,
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum HybridSource {
        BaseWord,
        SuffixWord,
        SuffixCharSingle,
        Model,
        BaseCharFirst,
        Passthrough,
    }

    impl HybridSource {
        fn as_str(self) -> &'static str {
            match self {
                HybridSource::BaseWord => "base_word",
                HybridSource::SuffixWord => "suffix_word",
                HybridSource::SuffixCharSingle => "suffix_char_single",
                HybridSource::Model => "model",
                HybridSource::BaseCharFirst => "base_char_first",
                HybridSource::Passthrough => "passthrough",
            }
        }
    }

    #[derive(Clone)]
    struct HybridCharDebug {
        ch: String,
        candidates: Vec<String>,
        chosen: String,
        confidence: Option<f32>,
        margin: Option<f32>,
        source: HybridSource,
    }

    #[derive(Clone)]
    struct HybridTokenDebug {
        token: String,
        source: HybridSource,
        output: String,
        chars: Vec<HybridCharDebug>,
    }

    #[derive(Clone)]
    struct HybridResult {
        output: String,
        tokens: Vec<HybridTokenDebug>,
        model_state: ModelState,
        suffix: Option<String>,
    }

    #[derive(Clone)]
    struct PendingModelChar {
        token_debug_idx: usize,
        char_debug_idx: usize,
        global_char_offset: usize,
        candidates: Vec<String>,
    }

    fn split_input(input: &str) -> Vec<Piece> {
        let mut pieces = Vec::new();
        let mut ascii_run = String::new();

        for ch in input.chars() {
            if ch.is_ascii_alphanumeric() {
                ascii_run.push(ch);
                continue;
            }

            if !ascii_run.is_empty() {
                pieces.push(Piece {
                    value: mem::take(&mut ascii_run),
                    kind: PieceKind::AsciiRun,
                });
            }

            pieces.push(Piece {
                value: ch.to_string(),
                kind: if ch.is_whitespace() {
                    PieceKind::Space
                } else {
                    PieceKind::Other
                },
            });
        }

        if !ascii_run.is_empty() {
            pieces.push(Piece {
                value: ascii_run,
                kind: PieceKind::AsciiRun,
            });
        }

        pieces
    }

    fn is_han_token(token: &str) -> bool {
        if token.is_empty() {
            return false;
        }

        let mut chars = token.chars();
        match (chars.next(), chars.next()) {
            (Some(ch), None) => !ch.is_whitespace() && !ch.is_ascii_alphanumeric(),
            _ => false,
        }
    }

    fn is_han_char(ch: char) -> bool {
        !ch.is_whitespace() && !ch.is_ascii_alphanumeric()
    }

    fn is_han_phrase(token: &str) -> bool {
        !token.is_empty() && token.chars().all(is_han_char)
    }

    fn romanize_plain_text_with_char_map(
        origin: &str,
        char_map: &HashMap<String, String>,
    ) -> String {
        let pieces = split_input(origin);
        let mut out = String::new();
        let mut last_is_space = true;

        for piece in pieces {
            match piece.kind {
                PieceKind::AsciiRun => {
                    out.push_str(&piece.value.to_ascii_lowercase());
                    last_is_space = false;
                }
                PieceKind::Space => {
                    if !last_is_space {
                        out.push(' ');
                        last_is_space = true;
                    }
                }
                PieceKind::Other => {
                    if char_map.contains_key(&piece.value) {
                        out.push_str(&piece.value);
                        last_is_space = false;
                    } else if !last_is_space {
                        out.push(' ');
                        last_is_space = true;
                    }
                }
            }
        }

        out.trim().to_string()
    }

    fn normalize_plain_text_preserving_chars(origin: &str) -> String {
        let pieces = split_input(origin);
        let mut out = String::new();
        let mut last_is_space = true;

        for piece in pieces {
            match piece.kind {
                PieceKind::AsciiRun => {
                    out.push_str(&piece.value.to_ascii_lowercase());
                    last_is_space = false;
                }
                PieceKind::Space => {
                    if !last_is_space {
                        out.push(' ');
                        last_is_space = true;
                    }
                }
                PieceKind::Other => {
                    out.push_str(&piece.value);
                    last_is_space = false;
                }
            }
        }

        out.trim().to_string()
    }

    fn tokenize_plain(romanized_text: &str) -> Vec<String> {
        if romanized_text.is_empty() {
            return Vec::new();
        }

        let mut tokens = Vec::new();
        let mut ascii_run = String::new();

        for ch in romanized_text.chars() {
            if ch.is_ascii_alphanumeric() {
                ascii_run.push(ch.to_ascii_lowercase());
                continue;
            }

            if !ascii_run.is_empty() {
                tokens.push(mem::take(&mut ascii_run));
            }

            if ch.is_whitespace() {
                continue;
            }

            tokens.push(ch.to_string());
        }

        if !ascii_run.is_empty() {
            tokens.push(ascii_run);
        }

        tokens
    }

    fn romanize_token_list(json_text: String) -> Vec<String> {
        match serde_json::from_str::<Vec<String>>(&json_text) {
            Ok(tokens) => tokens
                .into_iter()
                .map(|token| token.trim().to_string())
                .filter(|token| !token.is_empty())
                .map(|token| {
                    if token.chars().all(|ch| ch.is_ascii_alphanumeric()) {
                        token.to_ascii_lowercase()
                    } else {
                        token
                    }
                })
                .collect(),
            Err(err) => error!("failed to parse tokenizer result as JSON array: {err}"),
        }
    }

    fn pinyin_regex_phrase_patterns_impl(
        value: &str,
        generated_pinyin: bool,
    ) -> Option<Vec<String>> {
        with_token_dictionary_cache(|regex_tokens| {
            regex_phrase::pinyin_regex_phrase_patterns(value, generated_pinyin, regex_tokens)
        })
    }

    fn fetch_tokenizer_input_tokens(tokenizer_input: AnyElement) -> Option<Vec<String>> {
        let args =
            [unsafe { pgrx::datum::DatumWithOid::new(tokenizer_input, tokenizer_input.oid()) }];
        let json_text = match Spi::get_one_with_args::<String>(
            "SELECT COALESCE(jsonb_agg(token ORDER BY ord), '[]'::jsonb)::text \
             FROM unnest($1::text[]) WITH ORDINALITY AS t(token, ord)",
            &args,
        ) {
            Ok(Some(value)) => value,
            Ok(None) => "[]".to_string(),
            Err(_) => return None,
        };

        Some(romanize_token_list(json_text))
    }

    fn anyelement_to_text(tokenizer_input: AnyElement) -> Option<String> {
        let args =
            [unsafe { pgrx::datum::DatumWithOid::new(tokenizer_input, tokenizer_input.oid()) }];
        Spi::get_one_with_args::<String>("SELECT $1::text", &args)
            .ok()
            .flatten()
    }

    fn map_token(token: &str, char_map: &HashMap<String, String>) -> String {
        if token.chars().all(|ch| ch.is_ascii_alphanumeric()) {
            return token.to_ascii_lowercase();
        }

        if let Some(mapped) = char_map.get(token) {
            romanize_pinyin_phrase(mapped)
        } else {
            token.to_string()
        }
    }

    fn pinyin_char_romanize_with_char_map(
        origin: &str,
        char_map: &HashMap<String, String>,
    ) -> String {
        let romanized_text = romanize_plain_text_with_char_map(origin, char_map);
        let tokens = tokenize_plain(&romanized_text);

        if tokens.is_empty() {
            return String::new();
        }

        let mut out = Vec::with_capacity(tokens.len());
        for token in tokens {
            out.push(map_token(&token, char_map));
        }
        out.join(" ")
    }

    fn pinyin_char_romanize_impl(origin: &str) -> String {
        with_char_dictionary_cache(|char_map| pinyin_char_romanize_with_char_map(origin, char_map))
    }

    fn pinyin_char_romanize_with_suffix_impl(origin: &str, suffix: &str) -> String {
        match canonicalize_table_suffix(suffix) {
            Some(canonical_suffix) => with_suffix_char_cache(&canonical_suffix, |entry| {
                pinyin_char_romanize_with_char_map(origin, &entry.char_map)
            }),
            None => with_char_dictionary_cache(|char_map| {
                pinyin_char_romanize_with_char_map(origin, char_map)
            }),
        }
    }

    fn map_word_fallback(token: &str, char_map: &HashMap<String, String>) -> String {
        if token.chars().all(|ch| ch.is_ascii_alphanumeric()) {
            return token.to_ascii_lowercase();
        }

        if token.chars().count() == 1 {
            return map_token(token, char_map);
        }

        if !is_han_phrase(token) {
            return token.to_string();
        }

        let mut parts = Vec::new();
        for ch in token.chars() {
            parts.push(map_token(&ch.to_string(), char_map));
        }
        parts.join(" ")
    }

    fn romanize_word_tokens_with_maps(
        mut tokens: Vec<String>,
        char_map: &HashMap<String, String>,
        word_map: &HashMap<String, String>,
        max_word_len: usize,
    ) -> String {
        tokens.retain(|token| !token.is_empty());
        if tokens.is_empty() {
            return String::new();
        }

        let mut out = Vec::with_capacity(tokens.len());
        let mut idx = 0usize;

        while idx < tokens.len() {
            if let Some(mapped) = word_map.get(&tokens[idx]) {
                out.push(romanize_pinyin_phrase(mapped));
                idx += 1;
                continue;
            }

            if max_word_len >= 2 && is_han_token(&tokens[idx]) {
                let mut candidate = String::new();
                let max_end = usize::min(tokens.len(), idx + max_word_len);
                let mut best: Option<(usize, String)> = None;

                for end in idx..max_end {
                    if !is_han_token(&tokens[end]) {
                        break;
                    }

                    candidate.push_str(&tokens[end]);
                    let span = end - idx + 1;

                    if span >= 2 {
                        if let Some(mapped) = word_map.get(&candidate) {
                            best = Some((span, romanize_pinyin_phrase(mapped)));
                        }
                    }
                }

                if let Some((span, mapped)) = best {
                    out.push(mapped);
                    idx += span;
                    continue;
                }
            }

            out.push(map_word_fallback(&tokens[idx], char_map));
            idx += 1;
        }

        out.join(" ")
    }

    fn romanize_word_tokens(mut tokens: Vec<String>) -> String {
        tokens.retain(|token| !token.is_empty());
        if tokens.is_empty() {
            return String::new();
        }

        with_dictionary_cache(|cache| {
            romanize_word_tokens_with_maps(
                tokens,
                &cache.char_map,
                &cache.word_map,
                cache.max_word_len,
            )
        })
    }

    fn pinyin_word_romanize_with_maps(
        origin: &str,
        char_map: &HashMap<String, String>,
        word_map: &HashMap<String, String>,
        max_word_len: usize,
    ) -> String {
        let romanized_text = romanize_plain_text_with_char_map(origin, char_map);
        let tokens = tokenize_plain(&romanized_text);
        romanize_word_tokens_with_maps(tokens, char_map, word_map, max_word_len)
    }

    fn pinyin_word_romanize_impl(origin: &str) -> String {
        with_dictionary_cache(|cache| {
            pinyin_word_romanize_with_maps(
                origin,
                &cache.char_map,
                &cache.word_map,
                cache.max_word_len,
            )
        })
    }

    fn pinyin_word_romanize_with_suffix_impl(origin: &str, suffix: &str) -> String {
        match canonicalize_table_suffix(suffix) {
            Some(canonical_suffix) => with_suffix_word_cache(&canonical_suffix, |entry| {
                pinyin_word_romanize_with_maps(
                    origin,
                    &entry.char_map,
                    &entry.word_map,
                    entry.max_word_len,
                )
            }),
            None => with_dictionary_cache(|cache| {
                pinyin_word_romanize_with_maps(
                    origin,
                    &cache.char_map,
                    &cache.word_map,
                    cache.max_word_len,
                )
            }),
        }
    }

    fn pinyin_word_romanize_tokenizer_impl(tokenizer_input: AnyElement) -> String {
        if let Some(tokens) = fetch_tokenizer_input_tokens(tokenizer_input) {
            return romanize_word_tokens(tokens);
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => pinyin_word_romanize_impl(&text),
            None => error!("tokenizer input must be castable to text[] or text"),
        }
    }

    fn pinyin_word_romanize_tokenizer_with_suffix_impl(
        tokenizer_input: AnyElement,
        suffix: &str,
    ) -> String {
        let canonical_suffix = canonicalize_table_suffix(suffix);

        if let Some(tokens) = fetch_tokenizer_input_tokens(tokenizer_input) {
            return match canonical_suffix.as_deref() {
                Some(canonical_suffix) => with_suffix_word_cache(canonical_suffix, |entry| {
                    romanize_word_tokens_with_maps(
                        tokens,
                        &entry.char_map,
                        &entry.word_map,
                        entry.max_word_len,
                    )
                }),
                None => with_dictionary_cache(|cache| {
                    romanize_word_tokens_with_maps(
                        tokens,
                        &cache.char_map,
                        &cache.word_map,
                        cache.max_word_len,
                    )
                }),
            };
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => match canonical_suffix.as_deref() {
                Some(canonical_suffix) => with_suffix_word_cache(canonical_suffix, |entry| {
                    pinyin_word_romanize_with_maps(
                        &text,
                        &entry.char_map,
                        &entry.word_map,
                        entry.max_word_len,
                    )
                }),
                None => with_dictionary_cache(|cache| {
                    pinyin_word_romanize_with_maps(
                        &text,
                        &cache.char_map,
                        &cache.word_map,
                        cache.max_word_len,
                    )
                }),
            },
            None => error!("tokenizer input must be castable to text[] or text"),
        }
    }

    fn hybrid_word_lookup<'a>(
        word: &str,
        base_word_map: &'a HashMap<String, String>,
        suffix_word_map: Option<&'a HashMap<String, String>>,
    ) -> Option<(HybridSource, &'a str)> {
        if let Some(raw) = suffix_word_map.and_then(|map| map.get(word)) {
            return Some((HybridSource::SuffixWord, raw.as_str()));
        }
        base_word_map
            .get(word)
            .map(|raw| (HybridSource::BaseWord, raw.as_str()))
    }

    fn pick_token_source(chars: &[HybridCharDebug]) -> HybridSource {
        if chars.iter().any(|item| item.source == HybridSource::Model) {
            HybridSource::Model
        } else if chars
            .iter()
            .any(|item| item.source == HybridSource::SuffixCharSingle)
        {
            HybridSource::SuffixCharSingle
        } else if chars
            .iter()
            .any(|item| item.source == HybridSource::BaseCharFirst)
        {
            HybridSource::BaseCharFirst
        } else {
            HybridSource::Passthrough
        }
    }

    fn should_accept_model_decision(config: &ModelConfig, decision: &ModelDecision) -> bool {
        decision.confidence >= config.min_confidence && decision.margin >= config.min_margin
    }

    fn run_model_if_needed(
        model_state: &ModelState,
        req: &ModelRequest,
    ) -> Result<Option<Vec<ModelDecision>>, String> {
        if req.target_char_offsets.is_empty() {
            return Ok(None);
        }

        let Some(runtime) = model_state.runtime.as_ref() else {
            let reason = model_state
                .failed_message
                .clone()
                .unwrap_or_else(|| "no active model configured".to_string());
            if model_state.active_model.is_some() && !model_state.config.disable_on_error {
                return Err(reason);
            }
            return Ok(None);
        };

        match runtime.backend.disambiguate(req) {
            Ok(decisions) => Ok(Some(decisions)),
            Err(err) => {
                if runtime.config.disable_on_error {
                    Ok(None)
                } else {
                    Err(err)
                }
            }
        }
    }

    fn finalize_hybrid_token_debug(token_debug: &mut HybridTokenDebug) {
        if token_debug.chars.is_empty() {
            return;
        }

        token_debug.output = token_debug
            .chars
            .iter()
            .map(|item| item.chosen.clone())
            .collect::<Vec<_>>()
            .join(" ");
        token_debug.source = pick_token_source(&token_debug.chars);
    }

    fn prepare_hybrid_fallback(
        token_debug_idx: usize,
        token_char_offset: usize,
        token: &str,
        base_char_map: &HashMap<String, String>,
        suffix_char_map: Option<&HashMap<String, String>>,
    ) -> HybridTokenDebugWithPending {
        if token.chars().all(|ch| ch.is_ascii_alphanumeric()) {
            return HybridTokenDebugWithPending {
                token_debug: HybridTokenDebug {
                    token: token.to_string(),
                    source: HybridSource::Passthrough,
                    output: token.to_ascii_lowercase(),
                    chars: Vec::new(),
                },
                pending: Vec::new(),
            };
        }

        if !is_han_phrase(token) {
            return HybridTokenDebugWithPending {
                token_debug: HybridTokenDebug {
                    token: token.to_string(),
                    source: HybridSource::Passthrough,
                    output: token.to_string(),
                    chars: Vec::new(),
                },
                pending: Vec::new(),
            };
        }

        let chars: Vec<char> = token.chars().collect();
        let mut debug_chars = Vec::with_capacity(chars.len());
        let mut pending = Vec::new();

        for (idx, ch) in chars.iter().enumerate() {
            let ch_str = ch.to_string();
            if let Some(raw) = suffix_char_map.and_then(|map| map.get(&ch_str)) {
                let suffix_candidates = parse_candidates(raw);
                if suffix_candidates.len() == 1 {
                    debug_chars.push(HybridCharDebug {
                        ch: ch_str,
                        candidates: suffix_candidates.clone(),
                        chosen: suffix_candidates[0].clone(),
                        confidence: None,
                        margin: None,
                        source: HybridSource::SuffixCharSingle,
                    });
                    continue;
                }
            }

            let base_candidates = base_char_map
                .get(&ch_str)
                .map(|raw| parse_candidates(raw))
                .unwrap_or_default();

            if base_candidates.is_empty() {
                debug_chars.push(HybridCharDebug {
                    ch: ch_str.clone(),
                    candidates: Vec::new(),
                    chosen: ch_str,
                    confidence: None,
                    margin: None,
                    source: HybridSource::Passthrough,
                });
            } else if base_candidates.len() == 1 {
                debug_chars.push(HybridCharDebug {
                    ch: ch_str,
                    candidates: base_candidates.clone(),
                    chosen: base_candidates[0].clone(),
                    confidence: None,
                    margin: None,
                    source: HybridSource::BaseCharFirst,
                });
            } else {
                debug_chars.push(HybridCharDebug {
                    ch: ch_str,
                    candidates: base_candidates.clone(),
                    chosen: base_candidates[0].clone(),
                    confidence: None,
                    margin: None,
                    source: HybridSource::BaseCharFirst,
                });
                pending.push(PendingModelChar {
                    token_debug_idx,
                    char_debug_idx: idx,
                    global_char_offset: token_char_offset + idx,
                    candidates: base_candidates,
                });
            }
        }

        let mut token_debug = HybridTokenDebug {
            token: token.to_string(),
            source: HybridSource::Passthrough,
            output: String::new(),
            chars: debug_chars,
        };
        finalize_hybrid_token_debug(&mut token_debug);

        HybridTokenDebugWithPending {
            token_debug,
            pending,
        }
    }

    #[derive(Clone)]
    struct HybridTokenDebugWithPending {
        token_debug: HybridTokenDebug,
        pending: Vec<PendingModelChar>,
    }

    fn apply_sentence_model_decisions(
        sentence: &str,
        debug_tokens: &mut [HybridTokenDebug],
        pending: &[PendingModelChar],
        model_state: &ModelState,
    ) -> Result<(), String> {
        if pending.is_empty() {
            return Ok(());
        }

        let req = ModelRequest {
            sentence: sentence.to_string(),
            target_char_offsets: pending.iter().map(|item| item.global_char_offset).collect(),
            candidate_sets: pending.iter().map(|item| item.candidates.clone()).collect(),
        };
        let Some(decisions) = run_model_if_needed(model_state, &req)? else {
            return Ok(());
        };

        for (pending_item, decision) in pending.iter().zip(decisions.iter()) {
            let Some(token_debug) = debug_tokens.get_mut(pending_item.token_debug_idx) else {
                continue;
            };
            let Some(debug_char) = token_debug.chars.get_mut(pending_item.char_debug_idx) else {
                continue;
            };
            if should_accept_model_decision(&model_state.config, decision)
                && debug_char
                    .candidates
                    .iter()
                    .any(|candidate| candidate == &decision.chosen)
            {
                debug_char.chosen = decision.chosen.clone();
                debug_char.confidence = Some(decision.confidence);
                debug_char.margin = Some(decision.margin);
                debug_char.source = HybridSource::Model;
            }
        }

        for token_debug in debug_tokens.iter_mut() {
            finalize_hybrid_token_debug(token_debug);
        }

        Ok(())
    }

    fn romanize_word_tokens_hybrid_with_context(
        mut tokens: Vec<String>,
        base_char_map: &HashMap<String, String>,
        _merged_char_map: &HashMap<String, String>,
        base_word_map: &HashMap<String, String>,
        suffix_char_map: Option<&HashMap<String, String>>,
        suffix_word_map: Option<&HashMap<String, String>>,
        max_word_len: usize,
        model_state: &ModelState,
        suffix: Option<String>,
    ) -> Result<HybridResult, String> {
        tokens.retain(|token| !token.is_empty());
        if tokens.is_empty() {
            return Ok(HybridResult {
                output: String::new(),
                tokens: Vec::new(),
                model_state: model_state.clone(),
                suffix,
            });
        }

        let sentence = tokens.concat();
        let mut token_char_offsets = Vec::with_capacity(tokens.len());
        let mut running_offset = 0usize;
        for token in &tokens {
            token_char_offsets.push(running_offset);
            running_offset += token.chars().count();
        }

        let mut out = Vec::with_capacity(tokens.len());
        let mut debug_tokens = Vec::with_capacity(tokens.len());
        let mut idx = 0usize;
        let mut pending = Vec::new();

        while idx < tokens.len() {
            if let Some((source, raw)) =
                hybrid_word_lookup(&tokens[idx], base_word_map, suffix_word_map)
            {
                let output = romanize_pinyin_phrase(raw);
                debug_tokens.push(HybridTokenDebug {
                    token: tokens[idx].clone(),
                    source,
                    output: output.clone(),
                    chars: Vec::new(),
                });
                out.push(output);
                idx += 1;
                continue;
            }

            if max_word_len >= 2 && is_han_token(&tokens[idx]) {
                let mut candidate = String::new();
                let max_end = usize::min(tokens.len(), idx + max_word_len);
                let mut best: Option<(usize, HybridSource, String)> = None;

                for end in idx..max_end {
                    if !is_han_token(&tokens[end]) {
                        break;
                    }
                    candidate.push_str(&tokens[end]);
                    let span = end - idx + 1;
                    if span < 2 {
                        continue;
                    }

                    if let Some((source, raw)) =
                        hybrid_word_lookup(&candidate, base_word_map, suffix_word_map)
                    {
                        best = Some((span, source, romanize_pinyin_phrase(raw)));
                    }
                }

                if let Some((span, source, output)) = best {
                    debug_tokens.push(HybridTokenDebug {
                        token: tokens[idx..idx + span].concat(),
                        source,
                        output: output.clone(),
                        chars: Vec::new(),
                    });
                    out.push(output);
                    idx += span;
                    continue;
                }
            }

            let prepared = prepare_hybrid_fallback(
                debug_tokens.len(),
                token_char_offsets[idx],
                &tokens[idx],
                base_char_map,
                suffix_char_map,
            );
            pending.extend(prepared.pending);
            out.push(prepared.token_debug.output.clone());
            debug_tokens.push(prepared.token_debug);
            idx += 1;
        }

        apply_sentence_model_decisions(&sentence, &mut debug_tokens, &pending, model_state)?;
        out = debug_tokens
            .iter()
            .map(|token| token.output.clone())
            .collect();

        Ok(HybridResult {
            output: out.join(" "),
            tokens: debug_tokens,
            model_state: model_state.clone(),
            suffix,
        })
    }

    fn pinyin_word_romanize_hybrid_text_internal(
        origin: &str,
        suffix: Option<&str>,
        model: Option<&str>,
    ) -> Result<HybridResult, String> {
        let model_state = get_model_state_for_identifier(model)?;
        match suffix.and_then(canonicalize_table_suffix) {
            Some(canonical_suffix) => with_dictionary_cache(|base_cache| {
                with_suffix_word_cache(&canonical_suffix, |entry| {
                    let romanized_text = normalize_plain_text_preserving_chars(origin);
                    let tokens = tokenize_plain(&romanized_text);
                    romanize_word_tokens_hybrid_with_context(
                        tokens,
                        &base_cache.char_map,
                        &entry.char_map,
                        &base_cache.word_map,
                        Some(&entry.overlay_char_map),
                        Some(&entry.overlay_word_map),
                        entry.max_word_len,
                        &model_state,
                        Some(canonical_suffix.clone()),
                    )
                })
            }),
            None => with_dictionary_cache(|cache| {
                let romanized_text = normalize_plain_text_preserving_chars(origin);
                let tokens = tokenize_plain(&romanized_text);
                romanize_word_tokens_hybrid_with_context(
                    tokens,
                    &cache.char_map,
                    &cache.char_map,
                    &cache.word_map,
                    None,
                    None,
                    cache.max_word_len,
                    &model_state,
                    None,
                )
            }),
        }
    }

    fn pinyin_word_romanize_hybrid_tokens_internal(
        tokens: Vec<String>,
        suffix: Option<&str>,
        model: Option<&str>,
    ) -> Result<HybridResult, String> {
        let model_state = get_model_state_for_identifier(model)?;
        match suffix.and_then(canonicalize_table_suffix) {
            Some(canonical_suffix) => with_dictionary_cache(|base_cache| {
                with_suffix_word_cache(&canonical_suffix, |entry| {
                    romanize_word_tokens_hybrid_with_context(
                        tokens,
                        &base_cache.char_map,
                        &entry.char_map,
                        &base_cache.word_map,
                        Some(&entry.overlay_char_map),
                        Some(&entry.overlay_word_map),
                        entry.max_word_len,
                        &model_state,
                        Some(canonical_suffix.clone()),
                    )
                })
            }),
            None => with_dictionary_cache(|cache| {
                romanize_word_tokens_hybrid_with_context(
                    tokens,
                    &cache.char_map,
                    &cache.char_map,
                    &cache.word_map,
                    None,
                    None,
                    cache.max_word_len,
                    &model_state,
                    None,
                )
            }),
        }
    }

    fn pinyin_word_romanize_hybrid_impl(origin: &str, model: Option<&str>) -> String {
        match pinyin_word_romanize_hybrid_text_internal(origin, None, model) {
            Ok(result) => result.output,
            Err(err) => error!("{err}"),
        }
    }

    fn pinyin_word_romanize_hybrid_with_suffix_impl(
        origin: &str,
        suffix: &str,
        model: Option<&str>,
    ) -> String {
        match pinyin_word_romanize_hybrid_text_internal(origin, Some(suffix), model) {
            Ok(result) => result.output,
            Err(err) => error!("{err}"),
        }
    }

    fn pinyin_word_romanize_hybrid_tokenizer_impl(
        tokenizer_input: AnyElement,
        model: Option<&str>,
    ) -> String {
        if let Some(tokens) = fetch_tokenizer_input_tokens(tokenizer_input) {
            return match pinyin_word_romanize_hybrid_tokens_internal(tokens, None, model) {
                Ok(result) => result.output,
                Err(err) => error!("{err}"),
            };
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => pinyin_word_romanize_hybrid_impl(&text, model),
            None => error!("tokenizer input must be castable to text[] or text"),
        }
    }

    fn pinyin_word_romanize_hybrid_tokenizer_with_suffix_impl(
        tokenizer_input: AnyElement,
        suffix: &str,
        model: Option<&str>,
    ) -> String {
        if let Some(tokens) = fetch_tokenizer_input_tokens(tokenizer_input) {
            return match pinyin_word_romanize_hybrid_tokens_internal(tokens, Some(suffix), model) {
                Ok(result) => result.output,
                Err(err) => error!("{err}"),
            };
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => pinyin_word_romanize_hybrid_with_suffix_impl(&text, suffix, model),
            None => error!("tokenizer input must be castable to text[] or text"),
        }
    }

    fn debug_result_to_json(result: HybridResult, input: &str) -> Value {
        json!({
            "input": input,
            "suffix": result.suffix,
            "model": result.model_state.active_model,
            "model_status": result.model_state.status,
            "model_error": result.model_state.failed_message,
            "output": result.output,
            "tokens": result.tokens.into_iter().map(|token| {
                json!({
                    "token": token.token,
                    "source": token.source.as_str(),
                    "output": token.output,
                    "chars": token.chars.into_iter().map(|ch| {
                        json!({
                            "ch": ch.ch,
                            "candidates": ch.candidates,
                            "chosen": ch.chosen,
                            "confidence": ch.confidence,
                            "margin": ch.margin,
                            "source": ch.source.as_str(),
                        })
                    }).collect::<Vec<_>>()
                })
            }).collect::<Vec<_>>()
        })
    }

    fn pinyin_word_romanize_debug_impl(
        origin: &str,
        suffix: Option<&str>,
        model: Option<&str>,
    ) -> JsonB {
        match pinyin_word_romanize_hybrid_text_internal(origin, suffix, model) {
            Ok(result) => JsonB(debug_result_to_json(result, origin)),
            Err(err) => JsonB(json!({
                "input": origin,
                "suffix": suffix,
                "model": model,
                "model_status": "error",
                "model_error": err,
                "output": Value::Null,
                "tokens": [],
            })),
        }
    }

    fn model_arg_or_none(model: &str) -> Option<&str> {
        let trimmed = model.trim();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed)
        }
    }

    fn suffix_arg_or_none(suffix: &str) -> Option<&str> {
        let trimmed = suffix.trim();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed)
        }
    }

    fn pinyin_model_romanize_text_internal(
        origin: &str,
        model: &str,
    ) -> Result<HybridResult, String> {
        let model_state = get_model_state_for_identifier(Some(model))?;
        with_dictionary_cache(|cache| {
            let normalized_text = normalize_plain_text_preserving_chars(origin);
            let tokens = tokenize_plain(&normalized_text);
            romanize_word_tokens_hybrid_with_context(
                tokens,
                &cache.char_map,
                &cache.char_map,
                &HashMap::new(),
                None,
                None,
                0,
                &model_state,
                None,
            )
        })
    }

    fn pinyin_model_romanize_tokens_internal(
        tokenizer_input: AnyElement,
        model: &str,
    ) -> Result<HybridResult, String> {
        if let Some(tokens) = fetch_tokenizer_input_tokens(tokenizer_input) {
            let model_state = get_model_state_for_identifier(Some(model))?;
            return with_dictionary_cache(|cache| {
                romanize_word_tokens_hybrid_with_context(
                    tokens,
                    &cache.char_map,
                    &cache.char_map,
                    &HashMap::new(),
                    None,
                    None,
                    0,
                    &model_state,
                    None,
                )
            });
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => pinyin_model_romanize_text_internal(&text, model),
            None => Err("tokenizer input must be castable to text[] or text".to_string()),
        }
    }

    fn pinyin_model_romanize_impl(origin: &str, model: &str) -> String {
        match pinyin_model_romanize_text_internal(origin, model) {
            Ok(result) => result.output,
            Err(err) => error!("{err}"),
        }
    }

    fn pinyin_model_romanize_tokenizer_impl(tokenizer_input: AnyElement, model: &str) -> String {
        match pinyin_model_romanize_tokens_internal(tokenizer_input, model) {
            Ok(result) => result.output,
            Err(err) => error!("{err}"),
        }
    }

    fn pinyin_model_romanize_debug_impl(origin: &str, model: &str) -> JsonB {
        match pinyin_model_romanize_text_internal(origin, model) {
            Ok(result) => JsonB(debug_result_to_json(result, origin)),
            Err(err) => JsonB(json!({
                "input": origin,
                "model": model,
                "model_status": "error",
                "model_error": err,
                "output": Value::Null,
                "tokens": [],
            })),
        }
    }

    fn pinyin_char_target_debug_value(
        sentence: &str,
        query_char_offset: i32,
        suffix: Option<&str>,
    ) -> Result<Value, String> {
        if query_char_offset < 0 {
            return Err("query_char_offset must be >= 0".to_string());
        }
        let query_char_offset = query_char_offset as usize;
        let chars: Vec<char> = sentence.chars().collect();
        let Some(ch) = chars.get(query_char_offset) else {
            return Err(format!(
                "query_char_offset {} is out of range for sentence length {}",
                query_char_offset,
                chars.len()
            ));
        };
        let ch_str = ch.to_string();

        let resolve = |char_map: &HashMap<String, String>, source: &str| {
            let candidates = char_map
                .get(&ch_str)
                .map(|raw| parse_candidates(raw))
                .unwrap_or_default();
            let chosen = candidates
                .first()
                .cloned()
                .unwrap_or_else(|| ch_str.clone());
            json!({
                "sentence": sentence,
                "query_char_offset": query_char_offset,
                "ch": ch_str,
                "candidates": candidates,
                "chosen": chosen,
                "confidence": Value::Null,
                "margin": Value::Null,
                "source": source,
            })
        };

        Ok(match suffix.and_then(canonicalize_table_suffix) {
            Some(canonical_suffix) => with_suffix_char_cache(&canonical_suffix, |entry| {
                resolve(&entry.char_map, "char_dictionary")
            }),
            None => with_char_dictionary_cache(|char_map| resolve(char_map, "char_dictionary")),
        })
    }

    fn pinyin_word_target_debug_value(
        sentence: &str,
        query_char_offset: i32,
        suffix: Option<&str>,
        model: Option<&str>,
    ) -> Result<Value, String> {
        if query_char_offset < 0 {
            return Err("query_char_offset must be >= 0".to_string());
        }
        let query_char_offset = query_char_offset as usize;
        let model_state = get_model_state_for_identifier(model)?;

        let resolve = |base_char_map: &HashMap<String, String>,
                       base_word_map: &HashMap<String, String>,
                       suffix_char_map: Option<&HashMap<String, String>>,
                       suffix_word_map: Option<&HashMap<String, String>>,
                       max_word_len: usize|
         -> Result<Value, String> {
            let pieces = split_input(sentence);
            let mut tokens = Vec::new();
            let mut token_char_offsets = Vec::new();
            let mut running_offset = 0usize;
            for piece in pieces {
                let piece_len = piece.value.chars().count();
                match piece.kind {
                    PieceKind::Space => {}
                    PieceKind::AsciiRun => {
                        token_char_offsets.push(running_offset);
                        tokens.push(piece.value.to_ascii_lowercase());
                    }
                    PieceKind::Other => {
                        token_char_offsets.push(running_offset);
                        tokens.push(piece.value);
                    }
                }
                running_offset += piece_len;
            }
            if tokens.is_empty() {
                return Err("sentence produced no tokens".to_string());
            }

            let chars: Vec<char> = sentence.chars().collect();
            if query_char_offset >= chars.len() {
                return Err(format!(
                    "query_char_offset {} is out of range for sentence length {}",
                    query_char_offset,
                    chars.len()
                ));
            }

            let mut idx = 0usize;
            while idx < tokens.len() {
                let token_offset = token_char_offsets[idx];
                let token_len = tokens[idx].chars().count();

                if let Some((source, raw)) =
                    hybrid_word_lookup(&tokens[idx], base_word_map, suffix_word_map)
                {
                    if query_char_offset < token_offset + token_len {
                        let local_idx = query_char_offset - token_offset;
                        let chosen = romanize_pinyin_phrase(raw)
                            .split_whitespace()
                            .nth(local_idx)
                            .map(str::to_string)
                            .unwrap_or_else(|| {
                                tokens[idx]
                                    .chars()
                                    .nth(local_idx)
                                    .map(|ch| ch.to_string())
                                    .unwrap_or_default()
                            });
                        return Ok(json!({
                            "sentence": sentence,
                            "query_char_offset": query_char_offset,
                            "ch": chars[query_char_offset].to_string(),
                            "candidates": [chosen.clone()],
                            "chosen": chosen,
                            "confidence": Value::Null,
                            "margin": Value::Null,
                            "source": source.as_str(),
                            "token_source": source.as_str(),
                            "model": model_state.active_model,
                            "model_status": model_state.status,
                            "model_error": model_state.failed_message,
                        }));
                    }
                    idx += 1;
                    continue;
                }

                if max_word_len >= 2 && is_han_token(&tokens[idx]) {
                    let mut candidate = String::new();
                    let max_end = usize::min(tokens.len(), idx + max_word_len);
                    let mut best: Option<(usize, HybridSource, String)> = None;

                    for end in idx..max_end {
                        if !is_han_token(&tokens[end]) {
                            break;
                        }
                        candidate.push_str(&tokens[end]);
                        let span = end - idx + 1;
                        if span < 2 {
                            continue;
                        }

                        if let Some((source, raw)) =
                            hybrid_word_lookup(&candidate, base_word_map, suffix_word_map)
                        {
                            best = Some((span, source, romanize_pinyin_phrase(raw)));
                        }
                    }

                    if let Some((span, source, output)) = best {
                        let span_offset = token_offset;
                        let span_token = tokens[idx..idx + span].concat();
                        let span_len = span_token.chars().count();
                        if query_char_offset < span_offset + span_len {
                            let local_idx = query_char_offset - span_offset;
                            let chosen = output
                                .split_whitespace()
                                .nth(local_idx)
                                .map(str::to_string)
                                .unwrap_or_else(|| {
                                    span_token
                                        .chars()
                                        .nth(local_idx)
                                        .map(|ch| ch.to_string())
                                        .unwrap_or_default()
                                });
                            return Ok(json!({
                                "sentence": sentence,
                                "query_char_offset": query_char_offset,
                                "ch": chars[query_char_offset].to_string(),
                                "candidates": [chosen.clone()],
                                "chosen": chosen,
                                "confidence": Value::Null,
                                "margin": Value::Null,
                                "source": source.as_str(),
                                "token_source": source.as_str(),
                                "model": model_state.active_model,
                                "model_status": model_state.status,
                                "model_error": model_state.failed_message,
                            }));
                        }
                        idx += span;
                        continue;
                    }
                }

                if query_char_offset < token_offset + token_len {
                    let prepared = prepare_hybrid_fallback(
                        0,
                        token_offset,
                        &tokens[idx],
                        base_char_map,
                        suffix_char_map,
                    );
                    let mut resolved_tokens = vec![prepared.token_debug];
                    apply_sentence_model_decisions(
                        sentence,
                        &mut resolved_tokens,
                        &prepared.pending,
                        &model_state,
                    )?;
                    let resolved = resolved_tokens.remove(0);
                    let local_idx = query_char_offset - token_offset;
                    if let Some(char_debug) = resolved.chars.get(local_idx) {
                        return Ok(json!({
                            "sentence": sentence,
                            "query_char_offset": query_char_offset,
                            "ch": char_debug.ch,
                            "candidates": char_debug.candidates,
                            "chosen": char_debug.chosen,
                            "confidence": char_debug.confidence,
                            "margin": char_debug.margin,
                            "source": char_debug.source.as_str(),
                            "token_source": resolved.source.as_str(),
                            "model": model_state.active_model,
                            "model_status": model_state.status,
                            "model_error": model_state.failed_message,
                        }));
                    }

                    let chosen = resolved
                        .output
                        .split_whitespace()
                        .nth(local_idx)
                        .map(str::to_string)
                        .unwrap_or_else(|| {
                            tokens[idx]
                                .chars()
                                .nth(local_idx)
                                .map(|ch| ch.to_string())
                                .unwrap_or_default()
                        });
                    return Ok(json!({
                        "sentence": sentence,
                        "query_char_offset": query_char_offset,
                        "ch": chars[query_char_offset].to_string(),
                        "candidates": [chosen.clone()],
                        "chosen": chosen,
                        "confidence": Value::Null,
                        "margin": Value::Null,
                        "source": resolved.source.as_str(),
                        "token_source": resolved.source.as_str(),
                        "model": model_state.active_model,
                        "model_status": model_state.status,
                        "model_error": model_state.failed_message,
                    }));
                }

                idx += 1;
            }

            Err(format!(
                "query_char_offset {} could not be mapped to any token in '{}'",
                query_char_offset, sentence
            ))
        };

        match suffix.and_then(canonicalize_table_suffix) {
            Some(canonical_suffix) => with_dictionary_cache(|base_cache| {
                with_suffix_char_cache(&canonical_suffix, |entry| {
                    resolve(
                        &base_cache.char_map,
                        &base_cache.word_map,
                        Some(&entry.overlay_char_map),
                        Some(&entry.word_map),
                        base_cache.max_word_len.max(entry.max_word_len),
                    )
                })
            }),
            None => with_dictionary_cache(|cache| {
                resolve(
                    &cache.char_map,
                    &cache.word_map,
                    None,
                    None,
                    cache.max_word_len,
                )
            }),
        }
    }

    fn pinyin_polyphone_debug_value(
        sentence: &str,
        query_char_offset: i32,
        suffix: Option<&str>,
        model: Option<&str>,
    ) -> Result<Value, String> {
        if query_char_offset < 0 {
            return Err("query_char_offset must be >= 0".to_string());
        }
        let query_char_offset = query_char_offset as usize;
        let chars: Vec<char> = sentence.chars().collect();
        if chars.get(query_char_offset).is_none() {
            return Err(format!(
                "query_char_offset {} is out of range for sentence length {}",
                query_char_offset,
                chars.len()
            ));
        }
        let model_state = get_model_state_for_identifier(model)?;

        let resolve = |base_char_map: &HashMap<String, String>,
                       suffix_char_map: Option<&HashMap<String, String>>|
         -> Result<Value, String> {
            resolve_polyphone_target_value(
                sentence,
                query_char_offset,
                &chars,
                base_char_map,
                suffix_char_map,
                &model_state,
            )
        };

        match suffix.and_then(canonicalize_table_suffix) {
            Some(canonical_suffix) => with_dictionary_cache(|base_cache| {
                with_suffix_char_cache(&canonical_suffix, |entry| {
                    resolve(&base_cache.char_map, Some(&entry.overlay_char_map))
                })
            }),
            None => with_dictionary_cache(|cache| resolve(&cache.char_map, None)),
        }
    }

    fn resolve_polyphone_target_value(
        sentence: &str,
        query_char_offset: usize,
        chars: &[char],
        base_char_map: &HashMap<String, String>,
        suffix_char_map: Option<&HashMap<String, String>>,
        model_state: &ModelState,
    ) -> Result<Value, String> {
        let ch_str = chars
            .get(query_char_offset)
            .map(|ch| ch.to_string())
            .ok_or_else(|| {
                format!(
                    "query_char_offset {} is out of range for sentence length {}",
                    query_char_offset,
                    chars.len()
                )
            })?;

        if let Some(raw) = suffix_char_map.and_then(|map| map.get(&ch_str)) {
            let suffix_candidates = parse_candidates(raw);
            if suffix_candidates.len() == 1 {
                let chosen = suffix_candidates[0].clone();
                return Ok(json!({
                    "sentence": sentence,
                    "query_char_offset": query_char_offset,
                    "ch": ch_str,
                    "candidates": suffix_candidates,
                    "chosen": chosen,
                    "confidence": Value::Null,
                    "margin": Value::Null,
                    "source": HybridSource::SuffixCharSingle.as_str(),
                    "model": model_state.active_model,
                    "model_status": model_state.status,
                    "model_error": model_state.failed_message,
                }));
            }
        }

        let base_candidates = base_char_map
            .get(&ch_str)
            .map(|raw| parse_candidates(raw))
            .unwrap_or_default();

        if base_candidates.is_empty() {
            return Ok(json!({
                "sentence": sentence,
                "query_char_offset": query_char_offset,
                "ch": ch_str,
                "candidates": [],
                "chosen": ch_str,
                "confidence": Value::Null,
                "margin": Value::Null,
                "source": HybridSource::Passthrough.as_str(),
                "model": model_state.active_model,
                "model_status": model_state.status,
                "model_error": model_state.failed_message,
            }));
        }

        if base_candidates.len() == 1 {
            let chosen = base_candidates[0].clone();
            return Ok(json!({
                "sentence": sentence,
                "query_char_offset": query_char_offset,
                "ch": ch_str,
                "candidates": base_candidates,
                "chosen": chosen,
                "confidence": Value::Null,
                "margin": Value::Null,
                "source": HybridSource::BaseCharFirst.as_str(),
                "model": model_state.active_model,
                "model_status": model_state.status,
                "model_error": model_state.failed_message,
            }));
        }

        let req = ModelRequest {
            sentence: sentence.to_string(),
            target_char_offsets: vec![query_char_offset],
            candidate_sets: vec![base_candidates.clone()],
        };
        let model_decisions = run_model_if_needed(model_state, &req)?;
        let mut chosen = base_candidates[0].clone();
        let mut confidence = None;
        let mut margin = None;
        let mut source = HybridSource::BaseCharFirst;

        if let Some(decisions) = model_decisions {
            if let Some(decision) = decisions.first() {
                if should_accept_model_decision(&model_state.config, decision)
                    && base_candidates
                        .iter()
                        .any(|candidate| candidate == &decision.chosen)
                {
                    chosen = decision.chosen.clone();
                    confidence = Some(decision.confidence);
                    margin = Some(decision.margin);
                    source = HybridSource::Model;
                }
            }
        }

        Ok(json!({
            "sentence": sentence,
            "query_char_offset": query_char_offset,
            "ch": ch_str,
            "candidates": base_candidates,
            "chosen": chosen,
            "confidence": confidence,
            "margin": margin,
            "source": source.as_str(),
            "model": model_state.active_model,
            "model_status": model_state.status,
            "model_error": model_state.failed_message,
        }))
    }

    fn parse_benchmark_target_batch_payload(
        payload: &JsonB,
    ) -> Result<Vec<(String, usize)>, String> {
        let Some(items) = payload.0.as_array() else {
            return Err("benchmark payload must be a JSON array".to_string());
        };

        let mut out = Vec::with_capacity(items.len());
        for (idx, item) in items.iter().enumerate() {
            let Some(obj) = item.as_object() else {
                return Err(format!(
                    "benchmark payload item {} must be a JSON object",
                    idx
                ));
            };
            let sentence = obj
                .get("sentence")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    format!(
                        "benchmark payload item {} must contain string field 'sentence'",
                        idx
                    )
                })?
                .to_string();
            let query_char_offset = obj
                .get("query_char_offset")
                .and_then(Value::as_i64)
                .ok_or_else(|| {
                    format!(
                        "benchmark payload item {} must contain integer field 'query_char_offset'",
                        idx
                    )
                })?;
            if query_char_offset < 0 {
                return Err(format!(
                    "benchmark payload item {} has negative query_char_offset {}",
                    idx, query_char_offset
                ));
            }
            out.push((sentence, query_char_offset as usize));
        }

        Ok(out)
    }

    #[derive(Clone)]
    struct PendingBenchmarkTarget {
        output_idx: usize,
        query_char_offset: usize,
        candidates: Vec<String>,
    }

    fn resolve_benchmark_target_batch_for_sentence(
        sentence: &str,
        targets: &[(usize, usize)],
        base_char_map: &HashMap<String, String>,
        model_state: &ModelState,
    ) -> Result<Vec<String>, String> {
        let chars: Vec<char> = sentence.chars().collect();
        let mut chosen = vec![String::new(); targets.len()];
        let mut pending = Vec::new();

        for (output_idx, query_char_offset) in targets {
            let ch_str = chars
                .get(*query_char_offset)
                .map(|ch| ch.to_string())
                .ok_or_else(|| {
                    format!(
                        "query_char_offset {} is out of range for sentence length {}",
                        query_char_offset,
                        chars.len()
                    )
                })?;
            let base_candidates = base_char_map
                .get(&ch_str)
                .map(|raw| parse_candidates(raw))
                .unwrap_or_default();

            if base_candidates.is_empty() {
                chosen[*output_idx] = ch_str;
            } else if base_candidates.len() == 1 {
                chosen[*output_idx] = base_candidates[0].clone();
            } else {
                chosen[*output_idx] = base_candidates[0].clone();
                pending.push(PendingBenchmarkTarget {
                    output_idx: *output_idx,
                    query_char_offset: *query_char_offset,
                    candidates: base_candidates,
                });
            }
        }

        if pending.is_empty() {
            return Ok(chosen);
        }

        let req = ModelRequest {
            sentence: sentence.to_string(),
            target_char_offsets: pending.iter().map(|item| item.query_char_offset).collect(),
            candidate_sets: pending.iter().map(|item| item.candidates.clone()).collect(),
        };

        if let Some(decisions) = run_model_if_needed(model_state, &req)? {
            for (pending_item, decision) in pending.iter().zip(decisions.iter()) {
                if should_accept_model_decision(&model_state.config, decision)
                    && pending_item
                        .candidates
                        .iter()
                        .any(|candidate| candidate == &decision.chosen)
                {
                    chosen[pending_item.output_idx] = decision.chosen.clone();
                }
            }
        }

        Ok(chosen)
    }

    fn pinyin_benchmark_model_target_batch_length_impl(payload: JsonB, model: &str) -> i64 {
        let targets = match parse_benchmark_target_batch_payload(&payload) {
            Ok(targets) => targets,
            Err(err) => error!("{err}"),
        };
        let model_state = match get_model_state_for_identifier(Some(model)) {
            Ok(state) => state,
            Err(err) => error!("{err}"),
        };

        match with_dictionary_cache(|cache| {
            let mut total_length = 0i64;
            let mut grouped: BTreeMap<&str, Vec<(usize, usize)>> = BTreeMap::new();

            for (output_idx, (sentence, query_char_offset)) in targets.iter().enumerate() {
                grouped
                    .entry(sentence.as_str())
                    .or_default()
                    .push((output_idx, *query_char_offset));
            }

            for (sentence, sentence_targets) in grouped {
                let chosen = resolve_benchmark_target_batch_for_sentence(
                    sentence,
                    &sentence_targets,
                    &cache.char_map,
                    &model_state,
                )?;
                total_length += chosen.iter().map(|item| item.len() as i64).sum::<i64>();
            }
            Ok::<i64, String>(total_length)
        }) {
            Ok(total_length) => total_length,
            Err(err) => error!("{err}"),
        }
    }

    fn pinyin_polyphone_romanize_impl(
        sentence: &str,
        query_char_offset: i32,
        suffix: Option<&str>,
        model: Option<&str>,
    ) -> String {
        match pinyin_polyphone_debug_value(sentence, query_char_offset, suffix, model) {
            Ok(value) => value
                .get("chosen")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            Err(err) => error!("{err}"),
        }
    }

    fn pinyin_polyphone_debug_impl(
        sentence: &str,
        query_char_offset: i32,
        suffix: Option<&str>,
        model: Option<&str>,
    ) -> JsonB {
        match pinyin_polyphone_debug_value(sentence, query_char_offset, suffix, model) {
            Ok(value) => JsonB(value),
            Err(err) => JsonB(json!({
                "sentence": sentence,
                "query_char_offset": query_char_offset,
                "chosen": Value::Null,
                "source": "error",
                "model": model,
                "model_status": "error",
                "model_error": err,
            })),
        }
    }

    #[pg_extern(immutable, strict, parallel_safe)]
    fn pinyin_char_romanize(origin: &str) -> String {
        pinyin_char_romanize_impl(origin)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_char_romanize")]
    fn pinyin_char_romanize_with_suffix(origin: &str, suffix: &str) -> String {
        pinyin_char_romanize_with_suffix_impl(origin, suffix)
    }

    #[pg_extern(immutable, strict, parallel_safe)]
    fn pinyin_word_romanize(origin: &str) -> String {
        pinyin_word_romanize_impl(origin)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_word_romanize")]
    fn pinyin_word_romanize_with_suffix(origin: &str, suffix: &str) -> String {
        pinyin_word_romanize_with_suffix_impl(origin, suffix)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_word_romanize")]
    fn pinyin_word_romanize_with_tokenizer(tokenizer_input: AnyElement) -> String {
        pinyin_word_romanize_tokenizer_impl(tokenizer_input)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_word_romanize")]
    fn pinyin_word_romanize_with_tokenizer_and_suffix(
        tokenizer_input: AnyElement,
        suffix: &str,
    ) -> String {
        pinyin_word_romanize_tokenizer_with_suffix_impl(tokenizer_input, suffix)
    }

    #[pg_extern(stable, strict, parallel_safe, name = "pinyin_regex_phrase_patterns")]
    fn pinyin_regex_phrase_patterns_default(value: &str) -> Option<Vec<String>> {
        pinyin_regex_phrase_patterns_impl(value, false)
    }

    #[pg_extern(stable, strict, parallel_safe, name = "pinyin_regex_phrase_patterns")]
    fn pinyin_regex_phrase_patterns_with_generated(
        value: &str,
        generated_pinyin: bool,
    ) -> Option<Vec<String>> {
        pinyin_regex_phrase_patterns_impl(value, generated_pinyin)
    }

    #[pg_extern(
        stable,
        strict,
        parallel_unsafe,
        name = "pinyin__word_romanize_model_text"
    )]
    fn pinyin_word_romanize_model_text(origin: &str, model: &str) -> String {
        pinyin_word_romanize_hybrid_impl(origin, Some(model))
    }

    #[pg_extern(
        stable,
        strict,
        parallel_unsafe,
        name = "pinyin__word_romanize_model_text"
    )]
    fn pinyin_word_romanize_model_text_with_suffix(
        origin: &str,
        suffix: &str,
        model: &str,
    ) -> String {
        pinyin_word_romanize_hybrid_with_suffix_impl(origin, suffix, Some(model))
    }

    #[pg_extern(
        stable,
        strict,
        parallel_unsafe,
        name = "pinyin__word_romanize_model_tokenizer"
    )]
    fn pinyin_word_romanize_model_tokenizer(tokenizer_input: AnyElement, model: &str) -> String {
        pinyin_word_romanize_hybrid_tokenizer_impl(tokenizer_input, Some(model))
    }

    #[pg_extern(
        stable,
        strict,
        parallel_unsafe,
        name = "pinyin__word_romanize_model_tokenizer"
    )]
    fn pinyin_word_romanize_model_tokenizer_with_suffix(
        tokenizer_input: AnyElement,
        suffix: &str,
        model: &str,
    ) -> String {
        pinyin_word_romanize_hybrid_tokenizer_with_suffix_impl(tokenizer_input, suffix, Some(model))
    }

    #[pg_extern(stable, strict, parallel_unsafe)]
    fn pinyin_model_romanize(origin: &str, model: &str) -> String {
        pinyin_model_romanize_impl(origin, model)
    }

    #[pg_extern(stable, strict, parallel_unsafe, name = "pinyin_model_romanize")]
    fn pinyin_model_romanize_with_tokenizer(tokenizer_input: AnyElement, model: &str) -> String {
        pinyin_model_romanize_tokenizer_impl(tokenizer_input, model)
    }

    #[pg_extern(stable, strict, parallel_unsafe)]
    fn pinyin_word_romanize_debug(
        origin: &str,
        suffix: default!(&str, "''"),
        model: default!(&str, "''"),
    ) -> JsonB {
        pinyin_word_romanize_debug_impl(
            origin,
            suffix_arg_or_none(suffix),
            model_arg_or_none(model),
        )
    }

    #[pg_extern(stable, strict, parallel_unsafe)]
    fn pinyin_model_romanize_debug(origin: &str, model: &str) -> JsonB {
        pinyin_model_romanize_debug_impl(origin, model)
    }

    #[pg_extern(stable, strict, parallel_unsafe, name = "pinyin__char_target_romanize")]
    fn pinyin_char_target_romanize(
        sentence: &str,
        query_char_offset: i32,
        suffix: default!(&str, "''"),
    ) -> String {
        match pinyin_char_target_debug_value(
            sentence,
            query_char_offset,
            suffix_arg_or_none(suffix),
        ) {
            Ok(value) => value
                .get("chosen")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            Err(err) => error!("{err}"),
        }
    }

    #[pg_extern(stable, strict, parallel_unsafe, name = "pinyin__word_target_romanize")]
    fn pinyin_word_target_romanize(
        sentence: &str,
        query_char_offset: i32,
        suffix: default!(&str, "''"),
        model: default!(&str, "''"),
    ) -> String {
        match pinyin_word_target_debug_value(
            sentence,
            query_char_offset,
            suffix_arg_or_none(suffix),
            model_arg_or_none(model),
        ) {
            Ok(value) => value
                .get("chosen")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            Err(err) => error!("{err}"),
        }
    }

    #[pg_extern(
        stable,
        strict,
        parallel_unsafe,
        name = "pinyin__model_target_romanize"
    )]
    fn pinyin_model_target_romanize(sentence: &str, query_char_offset: i32, model: &str) -> String {
        pinyin_polyphone_romanize_impl(sentence, query_char_offset, None, Some(model))
    }

    #[pg_extern(stable, strict, parallel_unsafe, name = "pinyin__word_target_debug")]
    fn pinyin_word_target_debug(
        sentence: &str,
        query_char_offset: i32,
        suffix: default!(&str, "''"),
        model: default!(&str, "''"),
    ) -> JsonB {
        match pinyin_word_target_debug_value(
            sentence,
            query_char_offset,
            suffix_arg_or_none(suffix),
            model_arg_or_none(model),
        ) {
            Ok(value) => JsonB(value),
            Err(err) => JsonB(json!({
                "sentence": sentence,
                "query_char_offset": query_char_offset,
                "chosen": Value::Null,
                "source": "error",
                "model": model,
                "model_status": "error",
                "model_error": err,
            })),
        }
    }

    #[pg_extern(stable, strict, parallel_unsafe, name = "pinyin__model_target_debug")]
    fn pinyin_model_target_debug(sentence: &str, query_char_offset: i32, model: &str) -> JsonB {
        pinyin_polyphone_debug_impl(sentence, query_char_offset, None, Some(model))
    }

    #[pg_extern(
        stable,
        strict,
        parallel_unsafe,
        name = "pinyin__benchmark_model_target_batch_length"
    )]
    fn pinyin_benchmark_model_target_batch_length(payload: JsonB, model: &str) -> i64 {
        pinyin_benchmark_model_target_batch_length_impl(payload, model)
    }

    #[pg_extern(volatile, parallel_unsafe, name = "pinyin_clear_suffix_cache")]
    fn pinyin_clear_suffix_cache_all() -> i64 {
        clear_all_suffix_cache_impl()
    }

    #[pg_extern(volatile, strict, parallel_unsafe, name = "pinyin_clear_suffix_cache")]
    fn pinyin_clear_suffix_cache_for_suffix(suffix: &str) -> bool {
        clear_suffix_cache_impl(suffix)
    }

    #[pg_extern(volatile, parallel_unsafe, name = "pinyin__seed_embedded_data")]
    fn pinyin_seed_embedded_data_internal() -> bool {
        seed_embedded_dictionary_data();
        true
    }

    extension_sql!(
        r#"
        CREATE SCHEMA IF NOT EXISTS pinyin;

        CREATE TABLE IF NOT EXISTS pinyin.pinyin_mapping (
          character text PRIMARY KEY,
          pinyin text NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pinyin.pinyin_token (
          character text PRIMARY KEY,
          category smallint NOT NULL CHECK (category IN (1, 2, 3))
        );

        CREATE TABLE IF NOT EXISTS pinyin.pinyin_words (
          word text PRIMARY KEY,
          pinyin text NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pinyin.pinyin_dictionary_meta (
          singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
          version bigint NOT NULL DEFAULT 1
        );

        INSERT INTO pinyin.pinyin_dictionary_meta (singleton, version)
        VALUES (true, 1)
        ON CONFLICT (singleton) DO NOTHING;

        CREATE OR REPLACE FUNCTION pinyin.pinyin_dictionary_bump_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          UPDATE pinyin.pinyin_dictionary_meta
          SET version = version + 1
          WHERE singleton;
          RETURN NULL;
        END;
        $$;

        DROP TRIGGER IF EXISTS pinyin_mapping_bump_version ON pinyin.pinyin_mapping;
        CREATE TRIGGER pinyin_mapping_bump_version
        AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pinyin.pinyin_mapping
        FOR EACH STATEMENT
        EXECUTE FUNCTION pinyin.pinyin_dictionary_bump_version();

        DROP TRIGGER IF EXISTS pinyin_words_bump_version ON pinyin.pinyin_words;
        CREATE TRIGGER pinyin_words_bump_version
        AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pinyin.pinyin_words
        FOR EACH STATEMENT
        EXECUTE FUNCTION pinyin.pinyin_dictionary_bump_version();

        DROP TRIGGER IF EXISTS pinyin_token_bump_version ON pinyin.pinyin_token;
        CREATE TRIGGER pinyin_token_bump_version
        AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pinyin.pinyin_token
        FOR EACH STATEMENT
        EXECUTE FUNCTION pinyin.pinyin_dictionary_bump_version();

        INSERT INTO pinyin.pinyin_mapping (character, pinyin)
        VALUES (' ', ' ')
        ON CONFLICT (character) DO NOTHING;

        CREATE TABLE IF NOT EXISTS pinyin.pinyin_model_registry (
          model_name text PRIMARY KEY,
          kind text NOT NULL CHECK (kind IN ('g2pw_onnx', 'g2pm_numpy', 'small_onnx')),
          model_path text NOT NULL,
          tokenizer_path text,
          labels_path text,
          config jsonb NOT NULL DEFAULT '{}'::jsonb,
          enabled boolean NOT NULL DEFAULT true
        );

        CREATE TABLE IF NOT EXISTS pinyin.pinyin_model_meta (
          singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
          active_model text REFERENCES pinyin.pinyin_model_registry(model_name),
          version bigint NOT NULL DEFAULT 1
        );

        INSERT INTO pinyin.pinyin_model_meta (singleton, active_model, version)
        VALUES (true, NULL, 1)
        ON CONFLICT (singleton) DO NOTHING;

        CREATE OR REPLACE FUNCTION pinyin.pinyin_model_bump_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          UPDATE pinyin.pinyin_model_meta
          SET version = version + 1
          WHERE singleton;
          RETURN NULL;
        END;
        $$;

        CREATE OR REPLACE FUNCTION pinyin.pinyin_model_meta_set_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.active_model IS DISTINCT FROM OLD.active_model THEN
            NEW.version := OLD.version + 1;
          END IF;
          RETURN NEW;
        END;
        $$;

        DROP TRIGGER IF EXISTS pinyin_model_registry_bump_version ON pinyin.pinyin_model_registry;
        CREATE TRIGGER pinyin_model_registry_bump_version
        AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pinyin.pinyin_model_registry
        FOR EACH STATEMENT
        EXECUTE FUNCTION pinyin.pinyin_model_bump_version();

        DROP TRIGGER IF EXISTS pinyin_model_meta_set_version ON pinyin.pinyin_model_meta;
        CREATE TRIGGER pinyin_model_meta_set_version
        BEFORE UPDATE ON pinyin.pinyin_model_meta
        FOR EACH ROW
        EXECUTE FUNCTION pinyin.pinyin_model_meta_set_version();
        "#,
        name = "pinyin_dictionary_tables",
        requires = [pinyin_seed_embedded_data_internal],
        bootstrap
    );

    extension_sql!(
        r#"
        SELECT public.pinyin__seed_embedded_data();
        "#,
        name = "pinyin_dictionary_seed",
        requires = [pinyin_seed_embedded_data_internal]
    );

    extension_sql!(
        r#"
        INSERT INTO pinyin.pinyin_model_registry (
          model_name,
          kind,
          model_path,
          tokenizer_path,
          labels_path,
          config,
          enabled
        )
        SELECT
          'bundled_g2pm',
          'g2pm_numpy',
          '/usr/share/postgresql/'
            || (current_setting('server_version_num')::int / 10000)::text
            || '/extension/pg_pinyin/g2pm/manifest.json',
          NULL,
          NULL,
          '{"min_confidence":0.80,"min_margin":0.05,"disable_on_error":true}'::jsonb,
          true
        WHERE NOT EXISTS (
          SELECT 1
          FROM pinyin.pinyin_model_registry
          WHERE kind = 'g2pm_numpy'
            AND model_name <> 'bundled_g2pm'
        )
        ON CONFLICT (model_name) DO UPDATE
        SET kind = EXCLUDED.kind,
            model_path = EXCLUDED.model_path,
            tokenizer_path = EXCLUDED.tokenizer_path,
            labels_path = EXCLUDED.labels_path,
            config = EXCLUDED.config,
            enabled = true;
        "#,
        name = "pinyin_seed_bundled_g2pm"
    );

    extension_sql!(
        r#"
        DROP FUNCTION IF EXISTS public.pinyin_word_romanize_hybrid(text);
        DROP FUNCTION IF EXISTS public.pinyin_word_romanize_hybrid(text, text);
        DROP FUNCTION IF EXISTS public.pinyin_word_romanize_hybrid(anyelement);
        DROP FUNCTION IF EXISTS public.pinyin_word_romanize_hybrid(anyelement, text);
        "#,
        name = "pinyin_drop_hybrid_surface"
    );

    extension_sql!(
        r#"
        DO $$
        BEGIN
          CREATE DOMAIN pinyin.model_identifier AS text
            CHECK (VALUE IS NOT NULL AND btrim(VALUE) <> '');
        EXCEPTION
          WHEN duplicate_object THEN NULL;
        END
        $$;

        CREATE OR REPLACE FUNCTION public.pinyin_word_romanize(origin text, model pinyin.model_identifier)
        RETURNS text
        LANGUAGE sql
        STABLE
        STRICT
        PARALLEL UNSAFE
        AS $$
          SELECT public.pinyin__word_romanize_model_text(origin, model::text)
        $$;

        CREATE OR REPLACE FUNCTION public.pinyin_word_romanize(
          origin text,
          suffix text,
          model pinyin.model_identifier
        )
        RETURNS text
        LANGUAGE sql
        STABLE
        STRICT
        PARALLEL UNSAFE
        AS $$
          SELECT public.pinyin__word_romanize_model_text(origin, suffix, model::text)
        $$;

        CREATE OR REPLACE FUNCTION public.pinyin_word_romanize(
          tokenizer_input anyelement,
          model pinyin.model_identifier
        )
        RETURNS text
        LANGUAGE sql
        STABLE
        STRICT
        PARALLEL UNSAFE
        AS $$
          SELECT public.pinyin__word_romanize_model_tokenizer(tokenizer_input, model::text)
        $$;

        CREATE OR REPLACE FUNCTION public.pinyin_word_romanize(
          tokenizer_input anyelement,
          suffix text,
          model pinyin.model_identifier
        )
        RETURNS text
        LANGUAGE sql
        STABLE
        STRICT
        PARALLEL UNSAFE
        AS $$
          SELECT public.pinyin__word_romanize_model_tokenizer(tokenizer_input, suffix, model::text)
        $$;
        "#,
        name = "pinyin_word_model_wrappers",
        requires = [
            pinyin_word_romanize_model_text,
            pinyin_word_romanize_model_text_with_suffix,
            pinyin_word_romanize_model_tokenizer,
            pinyin_word_romanize_model_tokenizer_with_suffix
        ]
    );

    extension_sql!(
        r#"
        DO $pinyin_regex_phrase$
        BEGIN
          IF to_regtype('pdb.query') IS NOT NULL THEN
            EXECUTE $create_function$
              CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase(
                value text,
                slope integer DEFAULT NULL,
                max_expansions integer DEFAULT NULL,
                generated_pinyin boolean DEFAULT false
              )
              RETURNS pdb.query
              LANGUAGE plpgsql
              STABLE
              PARALLEL SAFE
              AS $function$
              DECLARE
                patterns text[];
              BEGIN
                patterns := public.pinyin_regex_phrase_patterns(value, generated_pinyin);

                IF patterns IS NULL THEN
                  RETURN NULL;
                END IF;

                IF cardinality(patterns) = 0 THEN
                  RETURN pdb.empty();
                END IF;

                IF cardinality(patterns) = 1 THEN
                  RETURN pdb.regex(patterns[1]);
                END IF;

                IF max_expansions IS NOT NULL THEN
                  RETURN pdb.regex_phrase(patterns, COALESCE(slope, 0), max_expansions);
                END IF;

                IF slope IS NOT NULL THEN
                  RETURN pdb.regex_phrase(patterns, slope);
                END IF;

                RETURN pdb.regex_phrase(patterns);
              END;
              $function$;
            $create_function$;
          ELSE
            EXECUTE $create_function$
              CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase(
                value text,
                slope integer DEFAULT NULL,
                max_expansions integer DEFAULT NULL,
                generated_pinyin boolean DEFAULT false
              )
              RETURNS text
              LANGUAGE plpgsql
              STABLE
              PARALLEL SAFE
              AS $function$
              BEGIN
                RAISE EXCEPTION
                  'public.pinyin_regex_phrase requires CREATE EXTENSION pg_search before CREATE EXTENSION pg_pinyin';
              END;
              $function$;
            $create_function$;
          END IF;
        END;
        $pinyin_regex_phrase$;
        "#,
        name = "pinyin_regex_phrase_pg_search_helper",
        requires = [pinyin_regex_phrase_patterns_with_generated]
    );

    #[cfg(any(test, feature = "pg_test"))]
    #[pg_schema]
    mod tests {
        use super::*;

        fn seed_minimal_data() {
            Spi::run(
                "TRUNCATE TABLE pinyin.pinyin_mapping; \
                 TRUNCATE TABLE pinyin.pinyin_token; \
                 TRUNCATE TABLE pinyin.pinyin_words;",
            )
            .expect("failed to truncate dictionary tables");

            Spi::run(
                "INSERT INTO pinyin.pinyin_mapping (character, pinyin) VALUES
                   (' ', ' '),
                   ('我', '|wo|'),
                   ('们', '|men|'),
                   ('重', '|tong|zhong|chong|'),
                   ('起', '|qi|'),
                   ('启', '|qi|'),
                   ('郑', '|zheng|'),
                   ('爽', '|shuang|'),
                   ('银', '|yin|'),
                   ('行', '|xing|hang|'),
                   ('长', '|chang|zhang|');",
            )
            .expect("failed to seed pinyin_mapping");

            Spi::run(
                "INSERT INTO pinyin.pinyin_words (word, pinyin) VALUES
                   ('郑爽', '|zheng| |shuang|'),
                   ('银行', '|yin| |hang|')
                 ON CONFLICT (word) DO UPDATE SET pinyin = EXCLUDED.pinyin;",
            )
            .expect("failed to seed pinyin_words");
        }

        fn seed_suffix_tables(suffix: &str) {
            let mapping_table = format!("pinyin.pinyin_mapping{}", suffix);
            let words_table = format!("pinyin.pinyin_words{}", suffix);

            Spi::run(&format!(
                "CREATE TABLE IF NOT EXISTS {mapping_table} (
                   character text PRIMARY KEY,
                   pinyin text NOT NULL
                 )"
            ))
            .expect("failed to create suffix mapping table");

            Spi::run(&format!(
                "CREATE TABLE IF NOT EXISTS {words_table} (
                   word text PRIMARY KEY,
                   pinyin text NOT NULL
                 )"
            ))
            .expect("failed to create suffix words table");

            Spi::run(&format!(
                "TRUNCATE TABLE {mapping_table}; TRUNCATE TABLE {words_table};"
            ))
            .expect("failed to truncate suffix tables");

            Spi::run(&format!(
                "INSERT INTO {mapping_table} (character, pinyin) VALUES
                   ('郑', '|zhengx|'),
                   ('重', '|zhong|'),
                   ('行', '|hang|xing|')
                 ON CONFLICT (character) DO UPDATE SET pinyin = EXCLUDED.pinyin"
            ))
            .expect("failed to seed suffix mapping");

            Spi::run(&format!(
                "INSERT INTO {words_table} (word, pinyin) VALUES
                   ('郑爽', '|zhengx| |shuangx|')
                 ON CONFLICT (word) DO UPDATE SET pinyin = EXCLUDED.pinyin"
            ))
            .expect("failed to seed suffix words");
        }

        fn clear_model_tables() {
            Spi::run(
                "TRUNCATE TABLE pinyin.pinyin_model_meta, pinyin.pinyin_model_registry;
                 INSERT INTO pinyin.pinyin_model_meta (singleton, active_model, version)
                 VALUES (true, NULL, 1)
                 ON CONFLICT (singleton) DO NOTHING;",
            )
            .expect("failed to reset model tables");
        }

        fn activate_mock_model_kind(name: &str, kind: &str, config: &str) {
            clear_model_tables();
            Spi::run(&format!(
                "INSERT INTO pinyin.pinyin_model_registry
                   (model_name, kind, model_path, tokenizer_path, labels_path, config, enabled)
                 VALUES
                   ({name}, {kind}, '/tmp/missing.onnx', NULL, NULL, {config}::jsonb, true)",
                name = sql_literal(name),
                kind = sql_literal(kind),
                config = sql_literal(config),
            ))
            .expect("failed to insert model registry row");
        }

        fn activate_mock_model(name: &str, config: &str) {
            activate_mock_model_kind(name, "g2pw_onnx", config);
        }

        fn clear_model_cache_for_tests() {
            let mut cache = model_cache()
                .write()
                .expect("model cache write lock poisoned");
            cache.clear();
        }

        #[pg_test]
        fn test_parse_candidates_dedupes_and_preserves_first() {
            assert_eq!(
                parse_candidates("|Tong|zhong|tong|"),
                vec!["tong".to_string(), "zhong".to_string()]
            );
            assert_eq!(romanize_first_candidate("|Tong|zhong|"), "tong");
        }

        #[pg_test]
        fn test_pinyin_char_romanize() {
            seed_minimal_data();

            let converted =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('我ABC们123')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(converted, "wo abc men 123");
        }

        #[pg_test]
        fn test_pinyin_word_romanize() {
            seed_minimal_data();

            let converted = Spi::get_one::<String>("SELECT public.pinyin_word_romanize('郑爽ABC')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(converted, "zheng shuang abc");
        }

        #[pg_test]
        fn test_pinyin_word_romanize_with_tokenizer_passthrough() {
            seed_minimal_data();

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize(ARRAY['郑爽', 'ABC']::text[])",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "zheng shuang abc");
        }

        #[pg_test]
        fn test_dictionary_update_reflected() {
            seed_minimal_data();

            let before = Spi::get_one::<String>("SELECT public.pinyin_char_romanize('我')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(before, "wo");

            Spi::run("UPDATE pinyin.pinyin_mapping SET pinyin='|wo2|' WHERE character='我'")
                .expect("failed to update mapping");

            let after = Spi::get_one::<String>("SELECT public.pinyin_char_romanize('我')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(after, "wo2");
        }

        #[pg_test]
        fn test_polyphone_first_reading() {
            seed_minimal_data();

            let converted = Spi::get_one::<String>("SELECT public.pinyin_char_romanize('重起')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(converted, "tong qi");
        }

        #[pg_test]
        fn test_word_romanize_with_model_keeps_dictionary_word_hit() {
            seed_minimal_data();
            activate_mock_model(
                "mock_word",
                r#"{"mock_char_decisions":{"行":{"chosen":"xing","confidence":0.99,"margin":0.8}}}"#,
            );

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('银行', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "yin hang");
        }

        #[pg_test]
        fn test_word_romanize_with_model_selects_candidate() {
            seed_minimal_data();
            activate_mock_model(
                "mock_choose",
                r#"{"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.95,"margin":0.4}}}"#,
            );

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "zhong qi");
        }

        #[pg_test]
        fn test_word_romanize_with_g2pm_alias_selects_candidate() {
            seed_minimal_data();
            activate_mock_model_kind(
                "mock_g2pm",
                "g2pm_numpy",
                r#"{"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.95,"margin":0.4}}}"#,
            );

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pm')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "zhong qi");
        }

        #[pg_test]
        fn test_word_model_low_confidence_falls_back() {
            seed_minimal_data();
            activate_mock_model(
                "mock_low_conf",
                r#"{"min_confidence":0.90,"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.50,"margin":0.4}}}"#,
            );

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "tong qi");
        }

        #[pg_test]
        fn test_word_model_low_margin_falls_back() {
            seed_minimal_data();
            activate_mock_model(
                "mock_low_margin",
                r#"{"min_margin":0.20,"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.95,"margin":0.01}}}"#,
            );

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "tong qi");
        }

        #[pg_test]
        fn test_word_romanize_with_model_requires_enabled_registry_match() {
            seed_minimal_data();
            clear_model_tables();

            let err = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize_debug('重启', model => 'g2pw')::text",
            )
            .expect("SPI failed")
            .expect("no row returned");
            let parsed: Value = serde_json::from_str(&err).expect("debug payload must be JSON");
            assert_eq!(parsed["model_status"], "error");
            assert!(
                parsed["model_error"]
                    .as_str()
                    .expect("model_error must be a string")
                    .contains("no enabled model found")
            );
        }

        #[pg_test]
        fn test_word_model_load_failure_falls_back() {
            seed_minimal_data();
            activate_mock_model("missing_model", r#"{}"#);

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "tong qi");
        }

        #[pg_test]
        fn test_word_romanize_with_model_suffix_char_single_override_works() {
            seed_minimal_data();
            seed_suffix_tables("_suffix1");
            activate_mock_model("mock_suffix_single", r#"{}"#);

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', suffix => '_suffix1', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "zhong qi");
        }

        #[pg_test]
        fn test_word_model_suffix_multi_candidate_falls_back() {
            seed_minimal_data();
            seed_suffix_tables("_suffix1");
            activate_mock_model(
                "mock_suffix_multi",
                r#"{"mock_char_decisions":{"行":{"chosen":"xing","confidence":0.95,"margin":0.4}}}"#,
            );

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('行长', suffix => '_suffix1', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "xing chang");
        }

        #[pg_test]
        fn test_model_cache_reloads_after_version_change() {
            seed_minimal_data();
            clear_model_cache_for_tests();
            activate_mock_model("broken", r#"{}"#);

            let before = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(before, "tong qi");

            Spi::run(
                "UPDATE pinyin.pinyin_model_registry
                 SET config = '{\"mock_char_decisions\":{\"重\":{\"chosen\":\"zhong\",\"confidence\":0.96,\"margin\":0.3}}}'::jsonb
                 WHERE model_name = 'broken'",
            )
            .expect("failed to update model registry");

            let after = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(after, "zhong qi");
        }

        #[pg_test]
        fn test_model_cache_reuses_same_backend_entry_for_same_kind() {
            seed_minimal_data();
            clear_model_cache_for_tests();
            activate_mock_model(
                "cache_reuse",
                r#"{"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.96,"margin":0.3}}}"#,
            );

            let first = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            let second = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");

            assert_eq!(first, "zhong qi");
            assert_eq!(second, "zhong qi");

            let cache = model_cache()
                .read()
                .expect("model cache read lock poisoned");
            assert_eq!(cache.len(), 1);
            assert!(cache.contains_key("g2pw_onnx"));
        }

        #[pg_test]
        fn test_model_cache_reloads_after_guc_change() {
            seed_minimal_data();
            clear_model_cache_for_tests();
            activate_mock_model(
                "guc_reload",
                r#"{"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.95,"margin":0.4}}}"#,
            );

            Spi::run("SET LOCAL pg_pinyin.g2pw_window_size = '0'")
                .expect("failed to reset g2pw window size override");
            let before = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(before, "zhong qi");

            let signature_before = {
                let cache = model_cache()
                    .read()
                    .expect("model cache read lock poisoned");
                cache
                    .get("g2pw_onnx")
                    .and_then(|state| state.guc_signature.clone())
                    .expect("cached g2pw state must include guc signature")
            };

            Spi::run("SET LOCAL pg_pinyin.g2pw_window_size = '16'")
                .expect("failed to override g2pw window size");
            let after = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('重启', model => 'g2pw')",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(after, "zhong qi");

            let cache = model_cache()
                .read()
                .expect("model cache read lock poisoned");
            let state = cache
                .get("g2pw_onnx")
                .expect("cached g2pw state must exist");
            let signature_after = state
                .guc_signature
                .clone()
                .expect("cached g2pw state must include guc signature");
            assert_ne!(signature_before, signature_after);
        }

        #[pg_test]
        fn test_word_romanize_debug_json_contains_sources() {
            seed_minimal_data();
            activate_mock_model(
                "mock_debug",
                r#"{"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.95,"margin":0.4}}}"#,
            );

            let payload = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize_debug('重启', model => 'g2pw')::text",
            )
            .expect("SPI failed")
            .expect("no row returned");

            let parsed: Value = serde_json::from_str(&payload).expect("debug payload must be JSON");
            assert_eq!(parsed["output"], "zhong qi");
            assert_eq!(parsed["tokens"][0]["source"], "model");
            assert_eq!(parsed["tokens"][0]["chars"][0]["source"], "model");
        }

        #[pg_test]
        fn test_model_romanize_bypasses_word_dictionary() {
            seed_minimal_data();
            activate_mock_model(
                "mock_model_only",
                r#"{"mock_char_decisions":{"行":{"chosen":"xing","confidence":0.99,"margin":0.8},"长":{"chosen":"zhang","confidence":0.99,"margin":0.8}}}"#,
            );

            let converted =
                Spi::get_one::<String>("SELECT public.pinyin_model_romanize('银行行长', 'g2pw')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(converted, "yin xing xing zhang");
        }

        #[pg_test]
        fn test_model_romanize_debug_reports_model_source() {
            seed_minimal_data();
            activate_mock_model(
                "mock_model_debug",
                r#"{"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.95,"margin":0.4}}}"#,
            );

            let payload = Spi::get_one::<String>(
                "SELECT public.pinyin_model_romanize_debug('重启', 'g2pw')::text",
            )
            .expect("SPI failed")
            .expect("no row returned");

            let parsed: Value = serde_json::from_str(&payload).expect("debug payload must be JSON");
            assert_eq!(parsed["output"], "zhong qi");
            assert_eq!(parsed["tokens"][0]["source"], "model");
        }

        #[pg_test]
        fn test_romanize_functions_have_expected_volatility() {
            let char_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_char_romanize(text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(char_volatile, "i");

            let word_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_romanize(text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(word_volatile, "i");

            let word_model_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_romanize(text,pinyin.model_identifier)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(word_model_volatile, "s");

            let model_only_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_model_romanize(text,text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(model_only_volatile, "s");

            let debug_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_romanize_debug(text,text,text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(debug_volatile, "s");
        }

        #[pg_test]
        fn test_generated_column_usage_raw_sql() {
            seed_minimal_data();

            Spi::run(
                "CREATE TEMP TABLE pinyin_generated_demo (
                   id bigserial PRIMARY KEY,
                   description text NOT NULL,
                   pinyin text GENERATED ALWAYS AS (public.pinyin_char_romanize(description)) STORED
                 )",
            )
            .expect("failed to create generated column demo table");

            Spi::run("INSERT INTO pinyin_generated_demo (description) VALUES ('郑爽ABC')")
                .expect("failed to insert demo row");

            let pinyin = Spi::get_one::<String>(
                "SELECT pinyin FROM pinyin_generated_demo WHERE description = '郑爽ABC'",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(pinyin, "zheng shuang abc");
        }

        #[pg_test]
        fn test_suffix_overlay_priority() {
            seed_minimal_data();
            seed_suffix_tables("_suffix1");

            let base_char = Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(base_char, "zheng shuang abc");

            let suffix_char =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(suffix_char, "zhengx shuang abc");

            let suffix_word = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('郑爽ABC', '_suffix1'::text)",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(suffix_word, "zhengx shuangx abc");
        }

        #[pg_test]
        fn test_suffix_overlay_update_reflected_after_cache_clear() {
            seed_minimal_data();
            seed_suffix_tables("_suffix1");

            let before =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(before, "zhengx shuang abc");

            Spi::run(
                "UPDATE pinyin.pinyin_mapping_suffix1
                 SET pinyin = '|zhengy|'
                 WHERE character = '郑'",
            )
            .expect("failed to update suffix mapping");

            let still_cached =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(still_cached, "zhengx shuang abc");

            let cleared =
                Spi::get_one::<bool>("SELECT public.pinyin_clear_suffix_cache('_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert!(cleared);

            let refreshed =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(refreshed, "zhengy shuang abc");
        }

        #[pg_test]
        fn test_suffix_overlay_fallback_to_base_when_missing() {
            seed_minimal_data();

            let base_word = Spi::get_one::<String>("SELECT public.pinyin_word_romanize('郑爽ABC')")
                .expect("SPI failed")
                .expect("no row returned");

            let missing_suffix_word = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize('郑爽ABC', '_nosuch'::text)",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(missing_suffix_word, base_word);
        }
    }
}

#[cfg(all(feature = "extension", any(test, feature = "pg_test")))]
pub mod pg_test {
    pub fn setup(_options: Vec<&str>) {}

    pub fn postgresql_conf_options() -> Vec<&'static str> {
        vec![]
    }
}
