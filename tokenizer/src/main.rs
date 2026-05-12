use std::io::{self, Read};

fn tokenize_zh(text: &str) -> Vec<String> {
    let jieba = jieba_rs::Jieba::new();
    jieba.cut(text, true).into_iter().map(|s| s.to_string()).collect()
}

fn tokenize_ja(text: &str) -> Vec<String> {
    let dictionary = lindera::dictionary::load_dictionary("embedded://ipadic")
        .expect("failed to load ipadic dictionary");
    let segmenter = lindera::segmenter::Segmenter::new(
        lindera::mode::Mode::Normal,
        dictionary,
        None,
    );
    let tokenizer = lindera::tokenizer::Tokenizer::new(segmenter);
    let tokens = tokenizer.tokenize(text).expect("tokenization failed");
    tokens.iter().map(|t| t.surface.as_ref().to_string()).collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();

    let mut lang = None;
    let mut json_output = false;
    let mut input_text = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--lang" => {
                i += 1;
                lang = Some(args[i].clone());
            }
            "--json" => {
                json_output = true;
            }
            other if !other.starts_with('-') => {
                input_text = Some(other.to_string());
            }
            _ => {
                eprintln!("unknown option: {}", args[i]);
                std::process::exit(1);
            }
        }
        i += 1;
    }

    let lang = lang.unwrap_or_else(|| {
        eprintln!("usage: dank-lookup-tokenize --lang zh|ja [--json] [TEXT]");
        std::process::exit(1);
    });

    let text = match input_text {
        Some(t) => t,
        None => {
            let mut buf = String::new();
            io::stdin().read_to_string(&mut buf).expect("failed to read stdin");
            buf
        }
    };
    let text = text.trim();

    if text.is_empty() {
        if json_output {
            println!("{{\"tokens\":[]}}");
        }
        return;
    }

    let tokens = match lang.as_str() {
        "zh" => tokenize_zh(text),
        "ja" => tokenize_ja(text),
        _ => {
            eprintln!("unsupported language: {} (use zh or ja)", lang);
            std::process::exit(1);
        }
    };

    if json_output {
        let obj = serde_json::json!({"tokens": tokens});
        println!("{}", serde_json::to_string(&obj).unwrap());
    } else {
        for token in &tokens {
            println!("{}", token);
        }
    }
}
