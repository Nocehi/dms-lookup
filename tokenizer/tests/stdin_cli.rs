use std::io::Write;
use std::process::{Command, Stdio};

#[test]
fn chinese_query_is_read_from_stdin_and_not_argv() {
    let executable = env!("CARGO_BIN_EXE_dank-lookup-tokenize");
    let query = "我來到北京清華大學";
    let mut child = Command::new(executable)
        .args(["--lang", "zh", "--json", "--stdin"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("spawn tokenizer");
    child
        .stdin
        .as_mut()
        .expect("tokenizer stdin")
        .write_all(query.as_bytes())
        .expect("write query");
    let output = child.wait_with_output().expect("wait for tokenizer");
    assert!(output.status.success());
    let value: serde_json::Value = serde_json::from_slice(&output.stdout).expect("JSON output");
    let joined = value["tokens"]
        .as_array()
        .expect("token array")
        .iter()
        .map(|item| item.as_str().expect("token string"))
        .collect::<String>();
    assert_eq!(joined, query);
}

#[test]
fn stdin_mode_rejects_a_positional_query() {
    let executable = env!("CARGO_BIN_EXE_dank-lookup-tokenize");
    let output = Command::new(executable)
        .args(["--lang", "zh", "--stdin", "secret"])
        .output()
        .expect("run tokenizer");
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("not supported"));
    assert!(!String::from_utf8_lossy(&output.stderr).contains("secret"));
}
