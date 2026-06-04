use std::env;
use std::fs;
use std::hint::black_box;
use std::path::PathBuf;
use std::time::Instant;

use pg_pinyin::regex_phrase::{
    RegexTokenDictionary, pinyin_regex_phrase_patterns, tokens_from_pinyin_token_csv,
};

const DEFAULT_QUERIES: [&str; 9] = [
    "lun",
    "lunlun",
    "zhengshuang",
    "wangchongyang",
    "xian",
    "wchy",
    "zh sh",
    "   ",
    "abc",
];

struct Args {
    rows: usize,
    runs: usize,
    token_file: PathBuf,
    queries: Vec<String>,
    mode: Mode,
}

#[derive(Clone, Copy)]
enum Mode {
    Tokens,
    Query,
}

struct RunResult {
    elapsed_ms: f64,
    nonnull: usize,
    checksum: usize,
    last_result: Option<String>,
}

fn parse_args() -> Args {
    let root_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut args = Args {
        rows: 20000,
        runs: 5,
        token_file: root_dir.join("sql/data/pinyin_token.csv"),
        queries: DEFAULT_QUERIES
            .iter()
            .map(|query| query.to_string())
            .collect(),
        mode: Mode::Tokens,
    };

    let mut iter = env::args().skip(1);
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--rows" => {
                args.rows = iter
                    .next()
                    .expect("--rows requires a value")
                    .parse()
                    .expect("--rows must be a non-negative integer");
            }
            "--runs" => {
                args.runs = iter
                    .next()
                    .expect("--runs requires a value")
                    .parse()
                    .expect("--runs must be a positive integer");
            }
            "--token-file" => {
                args.token_file =
                    PathBuf::from(iter.next().expect("--token-file requires a value"));
            }
            "--queries" => {
                args.queries = iter
                    .next()
                    .expect("--queries requires a value")
                    .split(',')
                    .map(str::to_string)
                    .collect();
            }
            "--mode" => {
                args.mode = match iter.next().expect("--mode requires a value").as_str() {
                    "tokens" => Mode::Tokens,
                    "query" => Mode::Query,
                    other => panic!("unsupported --mode {other}; expected tokens or query"),
                };
            }
            "--help" | "-h" => {
                println!(
                    "Usage: benchmark_pinyin_regex_phrase [--rows N] [--runs N] [--token-file PATH] [--queries CSV] [--mode tokens|query]"
                );
                std::process::exit(0);
            }
            other => panic!("unsupported argument: {other}"),
        }
    }

    if args.runs == 0 {
        panic!("--runs must be positive");
    }
    if args.queries.is_empty() {
        panic!("--queries must contain at least one query");
    }

    args
}

fn build_inputs(rows: usize, queries: &[String]) -> Vec<String> {
    (0..rows)
        .map(|idx| queries[idx % queries.len()].clone())
        .collect()
}

fn build_query_string(patterns: &[String]) -> String {
    if patterns.len() == 1 {
        format!("pdb.regex({:?})", patterns[0])
    } else {
        format!("pdb.regex_phrase({patterns:?})")
    }
}

fn run_once(inputs: &[String], dictionary: &RegexTokenDictionary, mode: Mode) -> RunResult {
    let start = Instant::now();
    let mut nonnull = 0usize;
    let mut checksum = 0usize;
    let mut last_result = None;

    for value in inputs {
        let result = pinyin_regex_phrase_patterns(value, false, dictionary);
        if let Some(patterns) = result {
            nonnull += 1;
            checksum += patterns.len();
            match mode {
                Mode::Tokens => {
                    checksum += patterns.iter().map(String::len).sum::<usize>();
                    last_result = Some(format!("{patterns:?}"));
                }
                Mode::Query => {
                    let query = build_query_string(&patterns);
                    checksum += query.len();
                    last_result = Some(query);
                }
            }
        }
    }

    RunResult {
        elapsed_ms: start.elapsed().as_secs_f64() * 1000.0,
        nonnull,
        checksum: black_box(checksum),
        last_result,
    }
}

fn main() {
    let args = parse_args();
    let token_csv = fs::read_to_string(&args.token_file).expect("failed to read token file");
    let dictionary = RegexTokenDictionary::from_tokens(tokens_from_pinyin_token_csv(&token_csv));
    let inputs = build_inputs(args.rows, &args.queries);

    for value in inputs.iter().take(1000) {
        let _ = pinyin_regex_phrase_patterns(value, false, &dictionary);
    }

    let mut runs = Vec::with_capacity(args.runs);
    for _ in 0..args.runs {
        runs.push(run_once(&inputs, &dictionary, args.mode));
    }

    let mut durations: Vec<f64> = runs.iter().map(|run| run.elapsed_ms).collect();
    durations.sort_by(f64::total_cmp);
    let best = durations[0];
    let median = durations[durations.len() / 2];
    let mean = durations.iter().sum::<f64>() / durations.len() as f64;
    let last = runs.last().expect("at least one run");
    let mode = match args.mode {
        Mode::Tokens => "tokens",
        Mode::Query => "query",
    };

    println!();
    println!("=== Query Builder Benchmark: Rust standalone pinyin_regex_phrase ===");
    println!("helper: pure Rust implementation");
    println!("mode: {mode}");
    println!("token_count: {}", dictionary.token_count());
    println!();
    println!("| metric | value |");
    println!("| --- | ---: |");
    println!("| rows | {} |", args.rows);
    println!("| non-null queries | {} |", last.nonnull);
    println!("| best | {best:.3} ms |");
    println!("| median | {median:.3} ms |");
    println!("| mean | {mean:.3} ms |");
    println!(
        "| best per row | {:.3} us |",
        best * 1000.0 / args.rows as f64
    );
    println!("| checksum | {} |", last.checksum);
    println!();
    println!(
        "runs_ms: {}",
        runs.iter()
            .map(|run| format!("{:.3}", run.elapsed_ms))
            .collect::<Vec<_>>()
            .join(", ")
    );
    println!(
        "last_result: {}",
        last.last_result.as_deref().unwrap_or("<null>")
    );
}
