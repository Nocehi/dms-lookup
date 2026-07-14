use std::io::{self, Read};

fn tokenize_zh(text: &str) -> Vec<String> {
    let jieba = jieba_rs::Jieba::new();
    jieba
        .cut(text, true)
        .into_iter()
        .map(|s| s.to_string())
        .collect()
}

fn tokenize_ja(text: &str) -> Vec<String> {
    let dictionary = lindera::dictionary::load_dictionary("embedded://ipadic")
        .expect("failed to load ipadic dictionary");
    let segmenter =
        lindera::segmenter::Segmenter::new(lindera::mode::Mode::Normal, dictionary, None);
    let tokenizer = lindera::tokenizer::Tokenizer::new(segmenter);
    let tokens = tokenizer.tokenize(text).expect("tokenization failed");
    tokens
        .iter()
        .map(|t| t.surface.as_ref().to_string())
        .collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();

    let mut lang = None;
    let mut json_output = false;
    let mut stdin_input = false;

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
            "--stdin" => {
                stdin_input = true;
            }
            _other if !_other.starts_with('-') => {
                eprintln!("positional text is not supported; write the query to stdin");
                std::process::exit(1);
            }
            _ => {
                eprintln!("unknown option: {}", args[i]);
                std::process::exit(1);
            }
        }
        i += 1;
    }

    let lang = lang.unwrap_or_else(|| {
        eprintln!("usage: dank-lookup-tokenize --lang zh|ja [--json] --stdin");
        std::process::exit(1);
    });

    if !stdin_input {
        eprintln!("--stdin is required; positional text is not supported");
        std::process::exit(1);
    }

    let mut text = String::new();
    io::stdin()
        .read_to_string(&mut text)
        .expect("failed to read stdin");
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

#[cfg(test)]
mod tests {
    use super::{tokenize_ja, tokenize_zh};

    #[test]
    fn chinese_tokens_preserve_the_input_text() {
        let input = "我來到北京清華大學";
        let tokens = tokenize_zh(input);
        assert!(!tokens.is_empty());
        assert_eq!(tokens.concat(), input);
    }

    #[test]
    fn japanese_tokens_preserve_the_input_text() {
        let input = "日本語の勉強";
        let tokens = tokenize_ja(input);
        assert!(!tokens.is_empty());
        assert_eq!(tokens.concat(), input);
    }
}
