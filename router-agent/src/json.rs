//! Zero-copy JSON scanner with byte spans, a backward parser for request tails, and in-place splicing.
//!
//! Strings are not decoded or validated while scanning: their contents are skipped with memchr, so a
//! multi-megabyte base64 image costs one SIMD scan. A string is decoded (and UTF-8-checked) only when
//! attribution inspects it.

use std::borrow::Cow;

pub const NONE: u32 = u32::MAX;
const MAX_DEPTH: usize = 900;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Obj,
    Arr,
    Str,
    Num,
    True,
    False,
    Null,
}

#[derive(Clone, Copy, Debug)]
pub struct Node {
    pub kind: Kind,
    /// Strings: content span (inside the quotes). Others: the full token / container span.
    pub s: u32,
    pub e: u32,
    /// Strings: contains a backslash escape (or may: the backward parser sets it and decode checks).
    pub esc: bool,
    /// Object members: the key's content span and whether it has escapes. NONE when not a member.
    pub key_s: u32,
    pub key_e: u32,
    pub key_esc: bool,
    pub first: u32,
    pub next: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    Syntax(usize),
    Depth,
}

impl ParseError {
    /// Label in the log's `attribution_failed:<name>` error.
    pub fn name(&self) -> &'static str {
        match self {
            ParseError::Syntax(_) => "JSONDecodeError",
            ParseError::Depth => "RecursionError",
        }
    }
}

pub struct Doc<'a> {
    pub src: Cow<'a, [u8]>,
    pub nodes: Vec<Node>,
    pub root: u32,
}

#[inline]
fn is_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r')
}

impl<'a> Doc<'a> {
    /// Structure only: string bytes are never inspected beyond finding their end.
    pub fn parse(src: Cow<'a, [u8]>) -> Result<Doc<'a>, ParseError> {
        let mut doc = Doc { src, nodes: Vec::new(), root: NONE };
        let src: &[u8] = &doc.src;
        let start = if src.starts_with(&[0xEF, 0xBB, 0xBF]) { 3 } else { 0 }; // UTF-8 BOM
        let mut p = Parser { b: src, i: start, nodes: Vec::with_capacity((src.len() / 512).clamp(64, 1 << 16)) };
        p.ws();
        let root = p.value(0)?;
        p.ws();
        if p.i != src.len() {
            return Err(ParseError::Syntax(p.i));
        }
        doc.nodes = p.nodes;
        doc.root = root;
        Ok(doc)
    }

    #[inline]
    pub fn node(&self, id: u32) -> &Node {
        &self.nodes[id as usize]
    }

    #[inline]
    pub fn kind(&self, id: u32) -> Kind {
        self.nodes[id as usize].kind
    }

    pub fn children(&self, id: u32) -> Children<'_, 'a> {
        Children { doc: self, cur: self.nodes[id as usize].first }
    }

    /// Raw bytes of a string node's contents (escapes not processed).
    #[inline]
    pub fn raw(&self, id: u32) -> &[u8] {
        let n = &self.nodes[id as usize];
        &self.src[n.s as usize..n.e as usize]
    }

    /// Decoded text of a string node.
    pub fn text(&self, id: u32) -> Cow<'_, str> {
        let n = &self.nodes[id as usize];
        decode(&self.src[n.s as usize..n.e as usize], n.esc)
    }

    pub fn key_text(&self, id: u32) -> Cow<'_, str> {
        let n = &self.nodes[id as usize];
        decode(&self.src[n.key_s as usize..n.key_e as usize], n.key_esc)
    }

    /// Object members in first-occurrence order; for a duplicate key the last value wins.
    pub fn members(&self, obj: u32) -> Vec<(Cow<'_, str>, u32)> {
        let mut out: Vec<(Cow<'_, str>, u32)> = Vec::new();
        for c in self.children(obj) {
            let k = self.key_text(c);
            if let Some(slot) = out.iter_mut().find(|(ek, _)| *ek == k) {
                slot.1 = c;
            } else {
                out.push((k, c));
            }
        }
        out
    }

    /// The member `key` of an object (the last one if the key repeats).
    pub fn get(&self, obj: u32, key: &str) -> Option<u32> {
        if self.kind(obj) != Kind::Obj {
            return None;
        }
        let mut found = None;
        for c in self.children(obj) {
            let n = self.node(c);
            let raw = &self.src[n.key_s as usize..n.key_e as usize];
            let hit = if n.key_esc { decode(raw, true) == key } else { raw == key.as_bytes() };
            if hit {
                found = Some(c);
            }
        }
        found
    }

    pub fn str_of(&self, id: Option<u32>) -> Option<Cow<'_, str>> {
        let id = id?;
        (self.kind(id) == Kind::Str).then(|| self.text(id))
    }
}

pub struct Children<'d, 'a> {
    doc: &'d Doc<'a>,
    cur: u32,
}

impl Iterator for Children<'_, '_> {
    type Item = u32;
    fn next(&mut self) -> Option<u32> {
        if self.cur == NONE {
            return None;
        }
        let c = self.cur;
        self.cur = self.doc.nodes[c as usize].next;
        Some(c)
    }
}

struct Parser<'b> {
    b: &'b [u8],
    i: usize,
    nodes: Vec<Node>,
}

impl Parser<'_> {
    #[inline]
    fn ws(&mut self) {
        while self.i < self.b.len() && is_ws(self.b[self.i]) {
            self.i += 1;
        }
    }

    fn push(&mut self, kind: Kind, s: usize, e: usize, esc: bool) -> u32 {
        self.nodes.push(Node { kind, s: s as u32, e: e as u32, esc, key_s: NONE, key_e: NONE, key_esc: false, first: NONE, next: NONE });
        (self.nodes.len() - 1) as u32
    }

    fn err<T>(&self) -> Result<T, ParseError> {
        Err(ParseError::Syntax(self.i))
    }

    fn value(&mut self, depth: usize) -> Result<u32, ParseError> {
        if depth > MAX_DEPTH {
            return Err(ParseError::Depth);
        }
        let Some(&c) = self.b.get(self.i) else { return self.err() };
        match c {
            b'{' => self.object(depth),
            b'[' => self.array(depth),
            b'"' => {
                let (s, e, esc) = self.string()?;
                Ok(self.push(Kind::Str, s, e, esc))
            }
            b't' => self.lit(b"true", Kind::True),
            b'f' => self.lit(b"false", Kind::False),
            b'n' => self.lit(b"null", Kind::Null),
            b'N' => self.lit(b"NaN", Kind::Num),
            b'I' => self.lit(b"Infinity", Kind::Num),
            b'-' if self.b[self.i..].starts_with(b"-Infinity") => self.lit(b"-Infinity", Kind::Num),
            b'-' | b'0'..=b'9' => self.number(),
            _ => self.err(),
        }
    }

    fn lit(&mut self, word: &[u8], kind: Kind) -> Result<u32, ParseError> {
        if self.b[self.i..].starts_with(word) {
            let s = self.i;
            self.i += word.len();
            Ok(self.push(kind, s, self.i, false))
        } else {
            self.err()
        }
    }

    fn number(&mut self) -> Result<u32, ParseError> {
        let s = self.i;
        let b = self.b;
        let mut i = self.i;
        if b.get(i) == Some(&b'-') {
            i += 1;
        }
        match b.get(i) {
            Some(b'0') => i += 1,
            Some(b'1'..=b'9') => {
                while matches!(b.get(i), Some(b'0'..=b'9')) {
                    i += 1;
                }
            }
            _ => {
                self.i = i;
                return self.err();
            }
        }
        if b.get(i) == Some(&b'.') && matches!(b.get(i + 1), Some(b'0'..=b'9')) {
            i += 1;
            while matches!(b.get(i), Some(b'0'..=b'9')) {
                i += 1;
            }
        }
        if matches!(b.get(i), Some(b'e' | b'E')) {
            let mut j = i + 1;
            if matches!(b.get(j), Some(b'+' | b'-')) {
                j += 1;
            }
            if matches!(b.get(j), Some(b'0'..=b'9')) {
                while matches!(b.get(j), Some(b'0'..=b'9')) {
                    j += 1;
                }
                i = j;
            }
        }
        self.i = i;
        Ok(self.push(Kind::Num, s, i, false))
    }

    /// Returns the content span and whether it contains escapes. Escapes must be valid (`\u` followed
    /// by four hex digits); the content is otherwise not inspected.
    fn string(&mut self) -> Result<(usize, usize, bool), ParseError> {
        let b = self.b;
        let s = self.i + 1;
        let mut i = s;
        let mut esc = false;
        loop {
            let rel = memchr::memchr2(b'"', b'\\', &b[i..]);
            let Some(rel) = rel else {
                self.i = b.len();
                return self.err();
            };
            i += rel;
            if b[i] == b'"' {
                self.i = i + 1;
                return Ok((s, i, esc));
            }
            esc = true;
            match b.get(i + 1) {
                Some(b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't') => i += 2,
                Some(b'u') if hex4(b, i + 2).is_some() => i += 6,
                _ => {
                    self.i = i;
                    return self.err();
                }
            }
        }
    }

    fn object(&mut self, depth: usize) -> Result<u32, ParseError> {
        let s = self.i;
        let id = self.push(Kind::Obj, s, s, false);
        self.i += 1;
        self.ws();
        let mut last = NONE;
        if self.b.get(self.i) == Some(&b'}') {
            self.i += 1;
            self.nodes[id as usize].e = self.i as u32;
            return Ok(id);
        }
        loop {
            if self.b.get(self.i) != Some(&b'"') {
                return self.err();
            }
            let (ks, ke, kesc) = self.string()?;
            self.ws();
            if self.b.get(self.i) != Some(&b':') {
                return self.err();
            }
            self.i += 1;
            self.ws();
            let child = self.value(depth + 1)?;
            {
                let n = &mut self.nodes[child as usize];
                n.key_s = ks as u32;
                n.key_e = ke as u32;
                n.key_esc = kesc;
            }
            if last == NONE {
                self.nodes[id as usize].first = child;
            } else {
                self.nodes[last as usize].next = child;
            }
            last = child;
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => {
                    self.i += 1;
                    self.ws();
                }
                Some(b'}') => {
                    self.i += 1;
                    self.nodes[id as usize].e = self.i as u32;
                    return Ok(id);
                }
                _ => return self.err(),
            }
        }
    }

    fn array(&mut self, depth: usize) -> Result<u32, ParseError> {
        let s = self.i;
        let id = self.push(Kind::Arr, s, s, false);
        self.i += 1;
        self.ws();
        let mut last = NONE;
        if self.b.get(self.i) == Some(&b']') {
            self.i += 1;
            self.nodes[id as usize].e = self.i as u32;
            return Ok(id);
        }
        loop {
            let child = self.value(depth + 1)?;
            if last == NONE {
                self.nodes[id as usize].first = child;
            } else {
                self.nodes[last as usize].next = child;
            }
            last = child;
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => {
                    self.i += 1;
                    self.ws();
                }
                Some(b']') => {
                    self.i += 1;
                    self.nodes[id as usize].e = self.i as u32;
                    return Ok(id);
                }
                _ => return self.err(),
            }
        }
    }
}

fn hex4(b: &[u8], i: usize) -> Option<u32> {
    let h = b.get(i..i + 4)?;
    let mut v = 0u32;
    for &c in h {
        v = v * 16 + (c as char).to_digit(16)?;
    }
    Some(v)
}

/// Decode a JSON string body, checking its UTF-8 here. Invalid UTF-8 is returned lossily with
/// escapes left as-is; lone surrogates become U+FFFD.
pub fn decode(raw: &[u8], esc: bool) -> Cow<'_, str> {
    let s: Cow<'_, str> = String::from_utf8_lossy(raw);
    if !esc || memchr::memchr(b'\\', raw).is_none() || matches!(s, Cow::Owned(_)) {
        return s;
    }
    let s: &str = match &s {
        Cow::Borrowed(b) => b,
        Cow::Owned(_) => unreachable!(),
    };
    let mut out = String::with_capacity(raw.len());
    let b = raw;
    let mut i = 0;
    while i < b.len() {
        let Some(rel) = memchr::memchr(b'\\', &b[i..]) else {
            out.push_str(&s[i..]);
            break;
        };
        out.push_str(&s[i..i + rel]);
        i += rel;
        match b.get(i + 1) {
            Some(b'"') => out.push('"'),
            Some(b'\\') => out.push('\\'),
            Some(b'/') => out.push('/'),
            Some(b'b') => out.push('\u{8}'),
            Some(b'f') => out.push('\u{c}'),
            Some(b'n') => out.push('\n'),
            Some(b'r') => out.push('\r'),
            Some(b't') => out.push('\t'),
            Some(b'u') => {
                let cp = hex4(b, i + 2).unwrap_or(0xFFFD);
                if (0xD800..0xDC00).contains(&cp) {
                    if b.get(i + 6) == Some(&b'\\') && b.get(i + 7) == Some(&b'u') {
                        if let Some(lo) = hex4(b, i + 8).filter(|lo| (0xDC00..0xE000).contains(lo)) {
                            let c = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            out.push(char::from_u32(c).unwrap_or('\u{FFFD}'));
                            i += 12;
                            continue;
                        }
                    }
                    out.push('\u{FFFD}');
                } else {
                    out.push(char::from_u32(cp).unwrap_or('\u{FFFD}'));
                }
                i += 6;
                continue;
            }
            _ => {}
        }
        i += 2;
    }
    Cow::Owned(out)
}

/// Encode a string as a JSON string literal (UTF-8 kept).
pub fn quote(s: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for c in s.chars() {
        match c {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\u{8}' => out.extend_from_slice(b"\\b"),
            '\u{c}' => out.extend_from_slice(b"\\f"),
            c if (c as u32) < 0x20 => out.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes()),
            c => {
                let mut buf = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
        }
    }
    out.push(b'"');
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(s: &str) -> Result<Doc<'_>, ParseError> {
        Doc::parse(Cow::Borrowed(s.as_bytes()))
    }

    fn splice(d: &Doc, repo: &str, source: &str) -> Option<Vec<u8>> {
        splice_plan(d, repo, source).map(|pl| apply_plan(&d.src, &pl))
    }

    #[test]
    fn parses_structure() {
        assert!(p(r#"{"a":[1,2.5e3,-0,true,false,null,"xé\n"]}"#).is_ok());
        assert!(p("NaN").is_ok() && p("-Infinity").is_ok());
        assert!(p("{\"a\":1,}").is_err());
        assert!(p("[1 2]").is_err());
        assert!(p(r#""\x""#).is_err());
        assert!(p("01").is_err());
        assert!(p("{} x").is_err());
        assert!(p("\u{feff}{}").is_ok());
        // string contents are not validated while scanning
        assert!(Doc::parse(Cow::Borrowed(b"\"\x01\xff\"")).is_ok());
    }

    #[test]
    fn decode_escapes() {
        let d = p(r#""a\"b\\c\/é😀\ud800x""#).unwrap();
        assert_eq!(d.text(d.root), "a\"b\\c/é😀\u{FFFD}x");
    }

    #[test]
    fn duplicate_keys_last_wins() {
        let d = p(r#"{"k":1,"k":"two"}"#).unwrap();
        assert_eq!(d.str_of(d.get(d.root, "k")).as_deref(), Some("two"));
        assert_eq!(d.members(d.root).len(), 1);
    }

    #[test]
    fn splice_cases() {
        let cases = [
            (r#"{"a":1}"#, r#"{"a":1,"metadata":{"repository":"o/r","repository_source":"git_remote"}}"#),
            (r#"{}"#, r#"{"metadata":{"repository":"o/r","repository_source":"git_remote"}}"#),
            (r#"{"metadata":null,"x":2}"#, r#"{"metadata":{"repository":"o/r","repository_source":"git_remote"},"x":2}"#),
            (r#"{"metadata":{"user_id":"u"} , "x":2}"#, r#"{"metadata":{"user_id":"u","repository":"o/r","repository_source":"git_remote"} , "x":2}"#),
            (r#"{"metadata":{}}"#, r#"{"metadata":{"repository":"o/r","repository_source":"git_remote"}}"#),
            (r#"{"metadata":{"repository":"","z":1}}"#, r#"{"metadata":{"repository":"o/r","z":1,"repository_source":"git_remote"}}"#),
        ];
        for (input, want) in cases {
            let d = p(input).unwrap();
            let got = splice(&d, "o/r", "git_remote").unwrap();
            assert_eq!(std::str::from_utf8(&got).unwrap(), want, "input {input}");
        }
        assert!(splice(&p("[1]").unwrap(), "o/r", "s").is_none());
        assert!(splice(&p(r#"{"metadata":5}"#).unwrap(), "o/r", "s").is_none());
    }
}

// ---------------------------------------------------------------- backward parsing (incremental mode)

/// Builds nodes for JSON values by reading backward from a value's end. Used to read only the tail of a
/// request (the members after the conversation and its newest messages). Assumes well-formed input; any
/// inconsistency returns an error and the caller falls back to a full forward parse.
pub struct RParser<'b, 'n> {
    pub b: &'b [u8],
    pub nodes: &'n mut Vec<Node>,
}

impl RParser<'_, '_> {
    #[inline]
    pub fn skip_ws_back(&self, mut e: usize) -> usize {
        while e > 0 && is_ws(self.b[e - 1]) {
            e -= 1;
        }
        e
    }

    fn push(&mut self, kind: Kind, s: usize, e: usize, esc: bool) -> u32 {
        self.nodes.push(Node { kind, s: s as u32, e: e as u32, esc, key_s: NONE, key_e: NONE, key_esc: false, first: NONE, next: NONE });
        (self.nodes.len() - 1) as u32
    }

    /// Start of the string whose closing quote is at `close`. Returns the opening quote's index.
    fn string_start(&self, close: usize) -> Result<usize, ParseError> {
        let mut j = close;
        loop {
            let k = memchr::memrchr(b'"', &self.b[..j]).ok_or(ParseError::Syntax(close))?;
            let mut bs = 0;
            while k > bs && self.b[k - 1 - bs] == b'\\' {
                bs += 1;
            }
            if bs % 2 == 0 {
                return Ok(k);
            }
            j = k;
        }
    }

    /// Parse the value ending at `e` (exclusive, no trailing whitespace). Returns (node, start).
    pub fn value(&mut self, e: usize, depth: usize) -> Result<(u32, usize), ParseError> {
        if depth > MAX_DEPTH {
            return Err(ParseError::Depth);
        }
        if e == 0 {
            return Err(ParseError::Syntax(0));
        }
        match self.b[e - 1] {
            b'"' => {
                let open = self.string_start(e - 1)?;
                // escape presence is resolved lazily at decode time (esc = true means "check")
                Ok((self.push(Kind::Str, open + 1, e - 1, true), open))
            }
            b'}' => self.container(e, depth, true),
            b']' => self.container(e, depth, false),
            c if c.is_ascii_alphanumeric() || c == b'.' || c == b'+' || c == b'-' => {
                let mut s = e;
                while s > 0 && matches!(self.b[s - 1], b'0'..=b'9' | b'a'..=b'z' | b'A'..=b'Z' | b'.' | b'+' | b'-') {
                    s -= 1;
                }
                let t = &self.b[s..e];
                let kind = match t {
                    b"true" => Kind::True,
                    b"false" => Kind::False,
                    b"null" => Kind::Null,
                    _ => Kind::Num,
                };
                Ok((self.push(kind, s, e, false), s))
            }
            _ => Err(ParseError::Syntax(e - 1)),
        }
    }

    fn container(&mut self, e: usize, depth: usize, obj: bool) -> Result<(u32, usize), ParseError> {
        let open = if obj { b'{' } else { b'[' };
        let mut kids: Vec<u32> = Vec::new();
        let mut pos = self.skip_ws_back(e - 1);
        if pos > 0 && self.b[pos - 1] == open {
            let id = self.push(if obj { Kind::Obj } else { Kind::Arr }, pos - 1, e, false);
            return Ok((id, pos - 1));
        }
        loop {
            let (v, vs) = self.value(pos, depth + 1)?;
            pos = self.skip_ws_back(vs);
            if obj {
                if pos == 0 || self.b[pos - 1] != b':' {
                    return Err(ParseError::Syntax(pos));
                }
                pos = self.skip_ws_back(pos - 1);
                if pos == 0 || self.b[pos - 1] != b'"' {
                    return Err(ParseError::Syntax(pos));
                }
                let kopen = self.string_start(pos - 1)?;
                let n = &mut self.nodes[v as usize];
                n.key_s = (kopen + 1) as u32;
                n.key_e = (pos - 1) as u32;
                n.key_esc = true;
                pos = self.skip_ws_back(kopen);
            }
            kids.push(v);
            if pos == 0 {
                return Err(ParseError::Syntax(0));
            }
            match self.b[pos - 1] {
                b',' => pos = self.skip_ws_back(pos - 1),
                c if c == open => {
                    let start = pos - 1;
                    let id = self.push(if obj { Kind::Obj } else { Kind::Arr }, start, e, false);
                    self.link(id, &kids);
                    return Ok((id, start));
                }
                _ => return Err(ParseError::Syntax(pos - 1)),
            }
        }
    }

    /// Link children collected in reverse order under `parent`, in document order.
    pub fn link(&mut self, parent: u32, kids_reversed: &[u32]) {
        let mut next = NONE;
        for &k in kids_reversed {
            self.nodes[k as usize].next = next;
            next = k;
        }
        self.nodes[parent as usize].first = next;
    }
}

/// Byte edits that set `metadata.repository` / `metadata.repository_source`, leaving every other byte
/// as received: an existing `metadata` object keeps its position and keys, existing `repository` /
/// `repository_source` values are replaced in place, a missing `metadata` is appended last, and a
/// null one is replaced. None when the root is not an object or `metadata` is another type.
/// Sent upstream as slices of the received buffer around the small inserts.
pub type Plan = Vec<(usize, usize, Vec<u8>)>;

pub fn splice_plan(doc: &Doc, repository: &str, source: &str) -> Option<Plan> {
    splice_fields(doc, &[("repository", repository), ("repository_source", source)])
}

/// Insert or replace string members in the request's metadata object.
pub fn splice_fields(doc: &Doc, fields: &[(&str, &str)]) -> Option<Plan> {
    if fields.is_empty() {
        return None;
    }
    let root = doc.root;
    if doc.kind(root) != Kind::Obj {
        return None;
    }
    let pair = |k: &str, v: &str| {
        let mut o = Vec::new();
        quote(k, &mut o);
        o.push(b':');
        quote(v, &mut o);
        o
    };
    let meta_obj = || {
        let mut o = vec![b'{'];
        for (i, (k, v)) in fields.iter().enumerate() {
            if i > 0 {
                o.push(b',');
            }
            o.extend(pair(k, v));
        }
        o.push(b'}');
        o
    };
    match doc.get(root, "metadata") {
        None => {
            let rn = doc.node(root);
            let close = rn.e as usize - 1;
            let mut ins = Vec::new();
            // Incremental documents omit the conversation's nodes, not its bytes.
            // Use the original object to decide whether an inserted member needs a comma.
            if doc.src[rn.s as usize + 1..close].iter().any(|b| !b.is_ascii_whitespace()) {
                ins.push(b',');
            }
            quote("metadata", &mut ins);
            ins.push(b':');
            ins.extend(meta_obj());
            Some(vec![(close, close, ins)])
        }
        Some(m) if doc.kind(m) == Kind::Null => {
            let n = doc.node(m);
            Some(vec![(n.s as usize, n.e as usize, meta_obj())])
        }
        Some(m) if doc.kind(m) == Kind::Obj => {
            let mut edits: Plan = Vec::new();
            let mut missing: Vec<(&str, &str)> = Vec::new();
            for &(k, v) in fields {
                match doc.get(m, k) {
                    Some(c) => {
                        let n = doc.node(c);
                        let (s, e) = if n.kind == Kind::Str { (n.s as usize - 1, n.e as usize + 1) } else { (n.s as usize, n.e as usize) };
                        let mut rep = Vec::new();
                        quote(v, &mut rep);
                        edits.push((s, e, rep));
                    }
                    None => missing.push((k, v)),
                }
            }
            let mn = doc.node(m);
            if !missing.is_empty() {
                let close = mn.e as usize - 1;
                let mut ins = Vec::new();
                for (i, (k, v)) in missing.iter().enumerate() {
                    if i > 0 || mn.first != NONE {
                        ins.push(b',');
                    }
                    ins.extend(pair(k, v));
                }
                edits.push((close, close, ins));
            }
            edits.sort_by_key(|e| e.0);
            Some(edits)
        }
        Some(_) => None,
    }
}

/// Apply a plan into one contiguous buffer (tests).
pub fn apply_plan(src: &[u8], plan: &Plan) -> Vec<u8> {
    let mut out = Vec::with_capacity(src.len() + 128);
    let mut pos = 0;
    for (s, e, rep) in plan {
        out.extend_from_slice(&src[pos..*s]);
        out.extend_from_slice(rep);
        pos = *e;
    }
    out.extend_from_slice(&src[pos..]);
    out
}
