//! Property tests: the zero-copy scanner accepts every document serde_json accepts and rejects
//! every structural error, and metadata splicing equals a reference merge on a parsed document
//! while leaving every other byte untouched.

use proptest::prelude::*;
use rrh::json::{apply_plan, splice_plan, Doc};
use serde_json::{Map, Value};
use std::borrow::Cow;

fn arb_value() -> impl Strategy<Value = Value> {
    let leaf = prop_oneof![
        Just(Value::Null),
        any::<bool>().prop_map(Value::Bool),
        any::<i64>().prop_map(|n| Value::from(n)),
        (-1e12f64..1e12).prop_map(|f| serde_json::Number::from_f64(f).map(Value::Number).unwrap_or(Value::Null)),
        "\\PC{0,24}".prop_map(Value::String),
        prop_oneof![Just("<cwd>/home/u/r</cwd>"), Just("Primary working directory: /Users/u/r"), Just("é😀\u{1}\"\\/")].prop_map(|s| Value::String(s.into())),
    ];
    leaf.prop_recursive(5, 64, 6, |inner| {
        prop_oneof![
            prop::collection::vec(inner.clone(), 0..6).prop_map(Value::Array),
            prop::collection::vec(("[a-z_]{1,8}", inner), 0..6).prop_map(|kv| {
                let mut m = Map::new();
                for (k, v) in kv {
                    m.insert(k, v);
                }
                Value::Object(m)
            }),
        ]
    })
}

fn arb_root() -> impl Strategy<Value = Value> {
    (prop::collection::vec(("[a-z]{1,6}", arb_value()), 0..5), prop_oneof![
        Just(None),
        Just(Some(Value::Null)),
        Just(Some(serde_json::json!({}))),
        Just(Some(serde_json::json!({"user_id": "u"}))),
        Just(Some(serde_json::json!({"repository": "", "z": 1}))),
        Just(Some(serde_json::json!({"repository_source": "old", "user_id": "u"}))),
    ])
        .prop_map(|(kv, meta)| {
            let mut m = Map::new();
            for (k, v) in kv {
                if k != "metadata" {
                    m.insert(k, v);
                }
            }
            if let Some(md) = meta {
                m.insert("metadata".into(), md);
            }
            Value::Object(m)
        })
}

/// Serialize with random-but-valid whitespace so splicing is tested against arbitrary formatting.
fn spaced(v: &Value, ws: &[u8]) -> String {
    let compact = serde_json::to_string(v).unwrap();
    let mut out = String::new();
    let mut in_str = false;
    let mut esc = false;
    let mut k = 0;
    for ch in compact.chars() {
        out.push(ch);
        if in_str {
            if esc { esc = false } else if ch == '\\' { esc = true } else if ch == '"' { in_str = false }
            continue;
        }
        if ch == '"' {
            in_str = true;
        } else if matches!(ch, '{' | '[' | ',' | ':') {
            let w = ws[k % ws.len()];
            k += 1;
            for _ in 0..(w % 3) {
                out.push(if w % 2 == 0 { ' ' } else { '\n' });
            }
        }
    }
    out
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(3000))]

    #[test]
    fn scanner_accepts_what_serde_accepts(v in arb_value(), ws in prop::collection::vec(any::<u8>(), 1..16)) {
        let text = spaced(&v, &ws);
        prop_assert!(Doc::parse(Cow::Borrowed(text.as_bytes())).is_ok(), "rejected valid JSON: {text}");
    }

    #[test]
    fn scanner_rejects_what_serde_rejects(v in arb_value(), cut in any::<usize>(), byte in any::<u8>()) {
        let mut b = serde_json::to_vec(&v).unwrap();
        if b.is_empty() { return Ok(()); }
        let i = cut % b.len();
        b[i] = byte;
        let serde = serde_json::from_slice::<Value>(&b);
        // numbers outside f64 range are fine structurally; serde_json rejects them
        if matches!(&serde, Err(e) if e.to_string().contains("number out of range")) { return Ok(()); }
        let serde_ok = serde.is_ok();
        let ours_ok = Doc::parse(Cow::Borrowed(&b)).is_ok();
        // the scanner also accepts NaN / Infinity tokens, and does not validate string contents
        // (raw control characters, invalid UTF-8); everything else must agree
        let ext = b.windows(3).any(|w| w == b"NaN") || b.windows(8).any(|w| w == b"Infinity");
        let content = byte < 0x20 || std::str::from_utf8(&b).is_err();
        if !ext && !(content && ours_ok) {
            prop_assert_eq!(serde_ok, ours_ok, "disagree on {:?}", String::from_utf8_lossy(&b));
        }
    }

    #[test]
    fn splice_equals_reference_merge(root in arb_root(), ws in prop::collection::vec(any::<u8>(), 1..16), repo in "[a-z]{1,8}/[a-z]{1,8}") {
        let text = spaced(&root, &ws);
        let doc = Doc::parse(Cow::Borrowed(text.as_bytes())).unwrap();
        let out = apply_plan(text.as_bytes(), &splice_plan(&doc, &repo, "git_remote").expect("object root"));
        let got: Value = serde_json::from_slice(&out).expect("spliced output parses");
        // reference: {**metadata, "repository": ..., "repository_source": ...}
        let mut want = root.clone();
        let obj = want.as_object_mut().unwrap();
        let mut md = match obj.get("metadata") { Some(Value::Object(m)) => m.clone(), _ => Map::new() };
        md.insert("repository".into(), Value::String(repo.clone()));
        md.insert("repository_source".into(), Value::String("git_remote".into()));
        obj.insert("metadata".into(), Value::Object(md));
        prop_assert_eq!(&got, &want);
        // key order too (preserve_order serde_json)
        prop_assert_eq!(serde_json::to_string(&got).unwrap(), serde_json::to_string(&want).unwrap());
        // nothing outside the metadata value changed
        let orig = text.as_bytes();
        let pre = orig.iter().zip(out.iter()).take_while(|(a, b)| a == b).count();
        let suf = orig.iter().rev().zip(out.iter().rev()).take_while(|(a, b)| a == b).count().min(orig.len() - pre).min(out.len() - pre);
        let changed_orig = &orig[pre..orig.len() - suf];
        prop_assert!(changed_orig.len() <= 64, "splice touched {} original bytes", changed_orig.len());
    }
}
