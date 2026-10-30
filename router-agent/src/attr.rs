//! Repository attribution for one model request. The working directory comes only from structured
//! places the harness itself writes (Claude Code and OpenCode `<env>`, Pi `<cwd>`, Codex turn
//! metadata and `<environment_context>`), never from tool output, file text, or arbitrary system text.
//! Tool calls then move it: edits and shell `cd` / `git -C` / `workdir` (strong), reads and paths in
//! commands (weak; used only when the stated directory is not a repository). Claude Code's shell is
//! persistent unless the harness reports a reset; other harnesses run each command where it says.
//! Requests that state nothing inherit the session's last repository, then fall back to the calling
//! process's directory.
//!
//! Requests are attributed incrementally: each conversation's state is cached, and the next request
//! is read backward only as far as the newest tool call already seen (see `tail_walk`).

use crate::git::{RepoResolver, Resolved};
use crate::json::{Doc, Kind, Plan};
use crate::paths;
use regex::Regex;
use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

pub const MAX_COMMAND_CHARS: usize = 20_000;

// `\s` here also matches U+001C..U+001F.
const S: &str = r"[\s\x1c-\x1f]";

/// Codex `<environment_context>` directory forms: `<cwd>`, "(Primary) working directory:", "cwd:".
static DIRECTORY_PATTERNS: LazyLock<[Regex; 3]> = LazyLock::new(|| {
    [
        Regex::new(&format!(r"<cwd>{S}*([^<\n]+?){S}*</cwd>")).unwrap(),
        Regex::new(&format!(r"(?im)^[\s\x1c-\x1f\-\*>]*(?:primary{S}+)?working director(?:y|ies):{S}*(/[^\n]+?){S}*$")).unwrap(),
        Regex::new(&format!(r"(?im)^[\s\x1c-\x1f\-\*>]*(?:cwd|current working directory):{S}*(/[^\n]+?){S}*$")).unwrap(),
    ]
});

/// Claude Code and OpenCode state their working directory inside an `<env>` block.
static ENV_BLOCK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)<env>(.*?)</env>").unwrap());
static ENV_DIRECTORY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"(?im)^[\s\x1c-\x1f\-\*>]*(?:primary{S}+)?working director(?:y|ies):{S}*(/[^\n]+?){S}*$")).unwrap()
});
/// Pi states its working directory in a `<cwd>` section.
static CWD_SECTION: LazyLock<Regex> = LazyLock::new(|| Regex::new(&format!(r"<cwd>{S}*([^<\n]+?){S}*</cwd>")).unwrap());

pub const TOOL_CALL_TYPES: [&str; 11] = [
    "function_call", "custom_tool_call", "local_shell_call", "shell_call", "apply_patch_call", "mcp_call",
    "computer_call", "tool_use", "server_tool_use", "toolCall", "tool-call",
];

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Attribution {
    pub repository: Option<String>,
    pub repository_host: Option<String>,
    pub repository_source: Option<String>,
    pub directory_source: Option<String>,
    pub directory_basename: Option<String>,
    pub cwd_repository: Option<String>,
    pub turn_id: Option<String>,
    pub turn_id_source: Option<String>,
    pub harness: Option<String>,
    pub client_pid: Option<i64>,
    pub client_command: Option<String>,
    pub error: Option<String>,
}

/// What attribution needs from the connection handler.
pub trait Ctx {
    fn header(&self, name: &str) -> Option<String>;
    /// The calling process's working directory (sets client_pid / client_command).
    fn calling_process(&self, att: &mut Attribution) -> Option<String>;
    fn resolver(&self) -> &RepoResolver;
    fn session_get(&self, id: &str) -> Option<(Resolved, String)>;
    fn session_put(&self, id: &str, value: (Resolved, String));
}

pub type R<T> = Result<T, &'static str>;

/// The newest stated working directory in a text.
fn directory_in_text(text: &str) -> Option<String> {
    let mut best: Option<(usize, String)> = None;
    for pat in DIRECTORY_PATTERNS.iter() {
        for c in pat.captures_iter(text) {
            let start = c.get(0).unwrap().start();
            let cand = paths::rstrip_chars(paths::strip(&c[1]), ".,;)");
            if cand.starts_with('/') && best.as_ref().map_or(true, |b| start > b.0) {
                best = Some((start, cand.to_string()));
            }
        }
    }
    best.map(|(_, c)| c)
}

/// The newest working directory in a structured harness block. Arbitrary `cwd:` lines in system
/// text are ignored, because instruction files and other system text can also contain them.
fn structured_directory_in_text(text: &str) -> Option<String> {
    let mut best: Option<(usize, String)> = None;
    for block in ENV_BLOCK.captures_iter(text) {
        let body = block.get(1).unwrap();
        for c in ENV_DIRECTORY.captures_iter(body.as_str()) {
            let start = body.start() + c.get(0).unwrap().start();
            let cand = paths::rstrip_chars(paths::strip(&c[1]), ".,;)");
            if cand.starts_with('/') && best.as_ref().map_or(true, |b| start > b.0) {
                best = Some((start, cand.to_string()));
            }
        }
    }
    for c in CWD_SECTION.captures_iter(text) {
        let start = c.get(0).unwrap().start();
        let cand = paths::rstrip_chars(paths::strip(&c[1]), ".,;)");
        if cand.starts_with('/') && best.as_ref().map_or(true, |b| start > b.0) {
            best = Some((start, cand.to_string()));
        }
    }
    best.map(|(_, c)| c)
}

pub fn is_tool_call(doc: &Doc, obj: u32) -> bool {
    if let Some(t) = doc.str_of(doc.get(obj, "type")) {
        if TOOL_CALL_TYPES.contains(&t.as_ref()) {
            return true;
        }
    }
    match doc.get(obj, "function") {
        Some(f) if doc.kind(f) == Kind::Obj => doc.get(f, "arguments").is_some(),
        _ => false,
    }
}

pub fn collect_tool_calls(doc: &Doc, id: u32, out: &mut Vec<u32>) {
    match doc.kind(id) {
        Kind::Obj => {
            if is_tool_call(doc, id) {
                out.push(id);
                return;
            }
            if has_dup_keys(doc, id) {
                for (_, c) in doc.members(id) {
                    collect_tool_calls(doc, c, out);
                }
            } else {
                for c in doc.children(id) {
                    collect_tool_calls(doc, c, out);
                }
            }
        }
        Kind::Arr => {
            for c in doc.children(id) {
                collect_tool_calls(doc, c, out);
            }
        }
        _ => {}
    }
}

/// Cheap duplicate-key check so the common case can iterate children directly.
pub fn has_dup_keys(doc: &Doc, obj: u32) -> bool {
    let n = doc.children(obj).count();
    if n < 2 {
        return false;
    }
    let mut keys: Vec<&[u8]> = doc.children(obj).map(|c| {
        let nd = doc.node(c);
        &doc.src[nd.key_s as usize..nd.key_e as usize]
    }).collect();
    let any_esc = doc.children(obj).any(|c| doc.node(c).key_esc);
    if any_esc {
        let mut dec: Vec<String> = doc.children(obj).map(|c| doc.key_text(c).into_owned()).collect();
        dec.sort();
        return dec.windows(2).any(|w| w[0] == w[1]);
    }
    keys.sort_unstable();
    keys.windows(2).any(|w| w[0] == w[1])
}

pub fn nearest_existing_directory(path: &str) -> Option<String> {
    let mut current = paths::rstrip_chars(path, ".:").to_string();
    for _ in 0..16 {
        if current.is_empty() || current == "/" {
            return None;
        }
        if paths::isdir(&current) {
            return Some(current);
        }
        current = paths::dirname(&current);
    }
    None
}

static SHELL_ASSIGNMENT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z_][A-Za-z0-9_]*=").unwrap());
static SHELL_RESET: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"Shell cwd was reset to (/[^\s]+)").unwrap());
static PATCH_FILE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$").unwrap());
/// The operator line of a command that is exactly `apply_patch <<EOF`.
static SOLE_PATCH_HEREDOC: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"^<<(-?)[ \t]*(?:'([A-Za-z_][A-Za-z0-9_]*)'|"([A-Za-z_][A-Za-z0-9_]*)"|\\?([A-Za-z_][A-Za-z0-9_]*))$"#).unwrap());
static WAS_SUFFIX: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+\(was [^)]*\)$").unwrap());

/// Last repository per harness session, for side requests and post-compaction turns.
pub struct SessionMemory {
    map: Mutex<HashMap<String, (Instant, Resolved, String)>>,
}

impl Default for SessionMemory {
    fn default() -> Self {
        Self { map: Mutex::new(HashMap::new()) }
    }
}

impl SessionMemory {
    const TTL: Duration = Duration::from_secs(6 * 3600);
    pub fn get(&self, id: &str) -> Option<(Resolved, String)> {
        let m = self.map.lock().unwrap();
        m.get(id).filter(|(t, _, _)| t.elapsed() < Self::TTL).map(|(_, r, d)| (r.clone(), d.clone()))
    }
    pub fn put(&self, id: &str, r: Resolved, dir: String) {
        let mut m = self.map.lock().unwrap();
        if m.len() >= 4096 {
            let cutoff = Instant::now() - Self::TTL;
            m.retain(|_, v| v.0 > cutoff);
            if m.len() >= 4096 {
                m.clear();
            }
        }
        m.insert(id.to_string(), (Instant::now(), r, dir));
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Harness {
    ClaudeCode,
    Codex,
    Generic,
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
enum Strength {
    Strong,
    Weak,
}

/// Text of a message `content`: a string, or the text of its text-like blocks.
fn content_texts<'d>(doc: &'d Doc, content: u32, out: &mut Vec<Cow<'d, str>>) {
    match doc.kind(content) {
        Kind::Str => out.push(doc.text(content)),
        Kind::Arr => {
            for b in doc.children(content) {
                if doc.kind(b) == Kind::Obj {
                    let t = doc.str_of(doc.get(b, "type"));
                    if matches!(t.as_deref(), Some("text" | "input_text" | "output_text")) {
                        if let Some(s) = doc.str_of(doc.get(b, "text")) {
                            out.push(s);
                        }
                    }
                } else if doc.kind(b) == Kind::Str {
                    out.push(doc.text(b));
                }
            }
        }
        _ => {}
    }
}

fn clean_dir(d: String) -> String {
    WAS_SUFFIX.replace(&d, "").into_owned()
}

fn header_json(ctx: &dyn Ctx, name: &str) -> Option<Doc<'static>> {
    let v = ctx.header(name)?;
    Doc::parse(Cow::Owned(v.into_bytes())).ok()
}

fn bounded_turn_id(value: Option<String>) -> Option<String> {
    value.filter(|v| !v.is_empty() && v.len() <= 256 && !v.chars().any(char::is_control))
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
struct UserTurns {
    count: u32,
    hash: u64,
    /// The newest conversation item is part of the current user turn's opening messages.
    open: bool,
}

fn stable_hash(mut h: u64, bytes: &[u8]) -> u64 {
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

fn harness_wrapper(text: &str) -> bool {
    let t = paths::strip(text);
    [
        "<system-reminder>", "<environment_context>", "<user_instructions>", "<subagent_notification>",
        "<in-app-browser-context", "# AGENTS.md instructions", "Base directory for this skill:",
        "[Request interrupted by user",
    ].iter().any(|p| t.starts_with(p))
}

/// Count user-authored messages. Tool results use the user role in some APIs, but do not start a turn.
fn user_turns(doc: &Doc, elems: &[u32], start: UserTurns, boundary_end: usize) -> (UserTurns, UserTurns) {
    let mut out = start;
    let mut at_boundary = start;
    for &item in elems {
        if doc.kind(item) != Kind::Obj {
            continue;
        }
        let role = doc.str_of(doc.get(item, "role"));
        if matches!(role.as_deref(), Some("system" | "developer")) {
            continue;
        }
        if role.as_deref() != Some("user") {
            out.open = false;
        } else if let Some(content) = doc.get(item, "content") {
            if doc.kind(content) == Kind::Arr && doc.children(content).any(|b| {
                doc.kind(b) == Kind::Obj && matches!(doc.str_of(doc.get(b, "type")).as_deref(), Some("tool_result" | "function_call_output"))
            }) {
                out.open = false;
            } else {
                let mut texts = Vec::new();
                content_texts(doc, content, &mut texts);
                let real: Vec<_> = texts.into_iter().filter(|t| !paths::strip(t).is_empty() && !harness_wrapper(t)).collect();
                let non_text: Vec<_> = if doc.kind(content) == Kind::Arr {
                    doc.children(content).filter(|&b| {
                        doc.kind(b) != Kind::Str && !(doc.kind(b) == Kind::Obj &&
                            matches!(doc.str_of(doc.get(b, "type")).as_deref(), Some("text" | "input_text" | "output_text")))
                    }).collect()
                } else if doc.kind(content) != Kind::Str { vec![content] } else { Vec::new() };
                if !real.is_empty() || !non_text.is_empty() {
                    let mut hash = if out.open { out.hash } else { 0xcbf2_9ce4_8422_2325 };
                    for t in real {
                        hash = stable_hash(hash, t.as_bytes());
                        hash = stable_hash(hash, b"\0");
                    }
                    for block in non_text {
                        hash = stable_hash(hash, doc.raw(block));
                        hash = stable_hash(hash, b"\0");
                    }
                    let count = if out.open { out.count } else { out.count.saturating_add(1) };
                    out = UserTurns { count, hash, open: true };
                }
            }
        }
        if doc.node(item).e as usize <= boundary_end {
            at_boundary = out;
        }
    }
    (out, at_boundary)
}

fn user_turns_full(doc: &Doc, h: Harness, boundary_end: usize) -> (UserTurns, UserTurns) {
    let lists: &[&str] = if h == Harness::Codex { &["input"] } else { &["messages", "input"] };
    for &key in lists {
        if let Some(arr) = doc.get(doc.root, key).filter(|&id| doc.kind(id) == Kind::Arr) {
            let elems: Vec<u32> = doc.children(arr).collect();
            return user_turns(doc, &elems, UserTurns::default(), boundary_end);
        }
    }
    (UserTurns::default(), UserTurns::default())
}

/// Prefer a harness turn ID. The fallback changes only when a new real user message appears.
fn turn_identity(doc: &Doc, ctx: &dyn Ctx, codex_md: Option<&Doc>, turns: UserTurns) -> (Option<String>, Option<String>) {
    if let Some(md) = codex_md {
        if let Some(id) = bounded_turn_id(md.str_of(md.get(md.root, "turn_id")).map(|v| v.into_owned())) {
            return (Some(id), Some("codex_turn_metadata".into()));
        }
    }
    if let Some(id) = bounded_turn_id(ctx.header("x-turn-id")) {
        return (Some(id), Some("harness_header".into()));
    }
    let Some(conversation) = conv_cache_key(ctx, codex_md).or_else(|| session_key(doc, ctx, codex_md)) else { return (None, None) };
    if turns.count == 0 {
        return (None, None);
    }
    let scope = stable_hash(0xcbf2_9ce4_8422_2325, conversation.as_bytes());
    (Some(format!("derived:{scope:016x}:{}:{:016x}", turns.count, turns.hash)), Some("derived_user_message".into()))
}

#[derive(Clone, Debug, PartialEq)]
struct Stated {
    dir: String,
    label: String,
}

/// Newest harness-authored directory among conversation elements, in document order: Codex's
/// `<environment_context>` messages, or structured system/developer blocks for everyone else.
fn msg_stated(doc: &Doc, elems: &[u32], h: Harness) -> Option<String> {
    match h {
        Harness::Codex => {
            let mut env_cwd: Option<String> = None;
            for &item in elems {
                if doc.kind(item) != Kind::Obj || doc.str_of(doc.get(item, "type")).as_deref() != Some("message") {
                    continue;
                }
                let mut texts = Vec::new();
                if let Some(ct) = doc.get(item, "content") {
                    content_texts(doc, ct, &mut texts);
                }
                for t in texts {
                    if paths::strip(&t).starts_with("<environment_context>") {
                        if let Some(d) = directory_in_text(&t) {
                            env_cwd = Some(d);
                        }
                    }
                }
            }
            env_cwd
        }
        Harness::ClaudeCode | Harness::Generic => {
            for &m in elems.iter().rev() {
                if doc.kind(m) != Kind::Obj {
                    continue;
                }
                let role = doc.str_of(doc.get(m, "role"));
                if !matches!(role.as_deref(), Some("system" | "developer")) {
                    continue;
                }
                let mut texts = Vec::new();
                if let Some(ct) = doc.get(m, "content") {
                    content_texts(doc, ct, &mut texts);
                }
                for t in texts.iter().rev() {
                    if let Some(d) = structured_directory_in_text(t) {
                        return Some(clean_dir(d));
                    }
                }
            }
            None
        }
    }
}

/// Top-level `system` / `instructions` (Claude Code <= 2.1.2xx and generic clients).
fn top_stated(doc: &Doc, h: Harness) -> Option<Stated> {
    if h == Harness::Codex {
        return None;
    }
    for key in ["system", "instructions"] {
        if let Some(s) = doc.get(doc.root, key) {
            let mut texts = Vec::new();
            content_texts(doc, s, &mut texts);
            for t in texts.iter().rev() {
                if let Some(d) = structured_directory_in_text(t) {
                    return Some(Stated { dir: clean_dir(d), label: format!("harness:{key}") });
                }
            }
        }
    }
    None
}

/// Fingerprint of the raw `system` / `instructions` members, so an unchanged system prompt is not
/// searched again on every request of a conversation.
fn top_fingerprint(doc: &Doc) -> u64 {
    let mut h: u64 = 0x9e37_79b9_7f4a_7c15;
    for key in ["system", "instructions"] {
        let Some(id) = doc.get(doc.root, key) else {
            h = (h ^ 0xff).wrapping_mul(0x1000_0000_01b3);
            continue;
        };
        let n = doc.node(id);
        let raw = &doc.src[n.s as usize..n.e as usize];
        h = (h ^ raw.len() as u64).wrapping_mul(0xff51_afd7_ed55_8ccd);
        let mut chunks = raw.chunks_exact(8);
        for c in &mut chunks {
            let w = u64::from_le_bytes(c.try_into().unwrap());
            h = (h ^ w).wrapping_mul(0xc4ce_b9fe_1a85_ec53).rotate_left(29);
        }
        for &b in chunks.remainder() {
            h = (h ^ b as u64).wrapping_mul(0x1000_0000_01b3);
        }
    }
    h
}

fn combine(h: Harness, msg: Option<String>, top: Option<Stated>, codex_md: Option<&Doc>) -> Option<Stated> {
    match h {
        Harness::Codex => {
            let env_cwd = msg;
            if let Some(md) = codex_md {
                if let Some(ws) = md.get(md.root, "workspaces").filter(|&w| md.kind(w) == Kind::Obj) {
                    let roots: Vec<String> = md.members(ws).into_iter().map(|(k, _)| k.into_owned()).collect();
                    let within = |cwd: &str, root: &str| cwd == root || cwd.starts_with(&format!("{}/", root.trim_end_matches('/')));
                    let pick = env_cwd
                        .as_ref()
                        .and_then(|cwd| roots.iter().filter(|r| within(cwd, r)).max_by_key(|r| r.len()).cloned())
                        .or_else(|| (roots.len() == 1).then(|| roots[0].clone()));
                    if let Some(p) = pick {
                        return Some(Stated { dir: env_cwd.clone().filter(|e| within(e, &p)).unwrap_or(p), label: "harness:codex_turn_metadata".into() });
                    }
                }
            }
            env_cwd.map(|d| Stated { dir: d, label: "harness:codex_environment_context".into() })
        }
        _ => msg.map(|d| Stated { dir: d, label: "harness:system_message".into() }).or(top),
    }
}

/// The conversation array's key and the newest directory stated in it, over the whole document.
fn msg_stated_full(doc: &Doc, h: Harness) -> (Option<&'static str>, Option<String>) {
    let lists: &[&'static str] = if h == Harness::Codex { &["input"] } else { &["messages", "input"] };
    let mut first_list = None;
    for &k in lists {
        let Some(arr) = doc.get(doc.root, k).filter(|&m| doc.kind(m) == Kind::Arr) else { continue };
        first_list.get_or_insert(k);
        let elems: Vec<u32> = doc.children(arr).collect();
        if let Some(d) = msg_stated(doc, &elems, h) {
            return (Some(k), Some(d));
        }
    }
    (first_list, None)
}

fn abs_from(base: Option<&str>, p: &str) -> Option<String> {
    let p = paths::strip_chars(p, "'\"");
    if p.is_empty() || p == "-" || p.contains('$') {
        return None;
    }
    if p.starts_with('~') {
        return Some(paths::normpath(&paths::expanduser(p)));
    }
    if p.starts_with('/') {
        return Some(paths::normpath(p));
    }
    base.map(|b| paths::normpath(&paths::join(b, p)))
}

fn dir_of_file(path: &str) -> Option<String> {
    nearest_existing_directory(path)
}

/// Shell command text from tool arguments (string or argv list).
fn command_text(doc: &Doc, args: u32) -> Option<String> {
    for key in ["command", "cmd"] {
        if let Some(v) = doc.get(args, key) {
            match doc.kind(v) {
                Kind::Str => return Some(doc.text(v).into_owned()),
                Kind::Arr => {
                    let parts: Vec<String> = doc.children(v).filter(|&x| doc.kind(x) == Kind::Str).map(|x| doc.text(x).into_owned()).collect();
                    return Some(parts.join(" "));
                }
                _ => {}
            }
        }
    }
    None
}

/// Skip a substitution without treating its contents as ordinary command arguments.
fn skip_substitution(chars: &mut std::iter::Peekable<std::str::CharIndices<'_>>, close: char, depth: usize) -> Option<()> {
    if depth >= 64 {
        return None;
    }
    let mut quote = None;
    let mut word = String::new();
    let mut command_start = true;
    let mut time_prefix = false;
    while let Some((_, c)) = chars.next() {
        if quote.is_none() && close != '}' {
            // This is a delimiter scanner, not a shell grammar parser. A case pattern's
            // unpaired ')' or a delimiter inside a comment/here-document could end the
            // substitution early and expose an unexecuted branch as ordinary arguments.
            if (word.is_empty() && c == '#') || (c == '<' && chars.peek().is_some_and(|&(_, next)| next == '<')) {
                return None;
            }
            if c == close || c.is_whitespace() || ";&|()<>{}".contains(c) {
                if !word.is_empty() {
                    // Reserved words are syntax only in command position, not in e.g.
                    // printf '%s' case. Compound-command prefixes keep that position open.
                    // Loop headers have their own command-position rules (for x do ...),
                    // so stop before them rather than exposing a case pattern's ')' later.
                    if command_start && matches!(word.as_str(), "case" | "[[" | "function" | "coproc" | "for" | "select") {
                        return None;
                    }
                    command_start = command_start && (matches!(word.as_str(), "if" | "then" | "else" | "elif" | "do" | "while" | "until" | "!" | "time")
                        || (time_prefix && matches!(word.as_str(), "-p" | "--")));
                    time_prefix = command_start && (word == "time" || time_prefix);
                    word.clear();
                }
                if ";&|\n".contains(c) {
                    command_start = true;
                    time_prefix = false;
                }
            } else if c != '\\' {
                word.push(c);
            }
        }
        match (quote, c) {
            (None, c) if c == close => return Some(()),
            (Some('\''), '\'') | (Some('"'), '"') => quote = None,
            (None, '\'' | '"') => quote = Some(c),
            (Some('\''), _) => {}
            (_, '\\') => {
                let (_, next) = chars.next()?;
                if quote.is_none() && next != '\n' {
                    word.push(next);
                }
            }
            (_, '`') => skip_substitution(chars, '`', depth + 1)?,
            (_, '$') => {
                if let Some(&(_, open @ ('(' | '{'))) = chars.peek() {
                    chars.next();
                    skip_substitution(chars, if open == '(' { ')' } else { '}' }, depth + 1)?;
                }
            }
            (None, '<' | '>') if chars.peek().is_some_and(|&(_, next)| next == '(') => {
                chars.next();
                skip_substitution(chars, ')', depth + 1)?;
                word.push('$'); // process substitution is an argument, not a new command
            }
            (None, '(') if close == ')' => {
                skip_substitution(chars, ')', depth + 1)?;
                command_start = true;
                time_prefix = false;
            }
            (None, '{') if close == '}' => skip_substitution(chars, '}', depth + 1)?,
            _ => {}
        }
    }
    None
}

/// Decode one complete argument. An expansion makes only its value unknown; its end offset is
/// still returned so later arguments can be scanned. Malformed/unfinished words return None.
fn shell_word(text: &str) -> Option<(Option<String>, usize)> {
    let mut word = String::new();
    let mut quote = None;
    let mut expanded = false;
    let mut chars = text.char_indices().peekable();
    let mut end = text.len();
    while let Some((i, c)) = chars.next() {
        match (quote, c) {
            (Some('\''), '\'') | (Some('"'), '"') => quote = None,
            (None, '\'' | '"') => quote = Some(c),
            (None | Some('"'), '\\') => {
                let (_, next) = chars.next()?;
                if quote == Some('"') && !matches!(next, '$' | '`' | '"' | '\\' | '\n') {
                    word.push('\\');
                }
                if next != '\n' {
                    word.push(next);
                }
            }
            (None | Some('"'), '$' | '`') => {
                expanded = true; // never guess expanded paths, but consume the whole argument
                if c == '`' {
                    skip_substitution(&mut chars, '`', 0)?;
                } else if let Some(&(_, open @ ('(' | '{'))) = chars.peek() {
                    chars.next();
                    skip_substitution(&mut chars, if open == '(' { ')' } else { '}' }, 0)?;
                }
            }
            (None, c) if c.is_whitespace() || ";&|()<>".contains(c) => {
                end = i;
                break;
            }
            _ => word.push(c),
        }
    }
    quote.is_none().then(|| ((!expanded && !word.is_empty()).then_some(word), end))
}

/// Raw shell tokens for finding compound-command boundaries, never for executing their bodies.
/// Quoted/expanded words remain whole, so a literal 'done' or ']]' cannot close a construct.
fn shell_token(mut rest: &str) -> Option<(&str, &str)> {
    loop {
        rest = rest.trim_start_matches(|c: char| c.is_whitespace() && c != '\n');
        if !rest.starts_with('#') { break; }
        rest = &rest[rest.find('\n').unwrap_or(rest.len())..];
    }
    let c = rest.chars().next()?;
    if ";&|()<>\n".contains(c) {
        return Some((&rest[..c.len_utf8()], &rest[c.len_utf8()..]));
    }
    let (_, end) = shell_word(rest)?;
    // A line continuation can turn separate-looking fragments into a reserved word.
    // Leave these uncommon compound forms unsupported rather than guessing a boundary.
    if rest[..end].contains("\\\n") { return None; }
    (end > 0).then(|| (&rest[..end], &rest[end..]))
}

/// `rest` starts just after an opening '('. Keep the same safety/depth limits as substitutions.
fn skip_shell_group(rest: &str) -> Option<&str> {
    let mut chars = rest.char_indices().peekable();
    skip_substitution(&mut chars, ')', 0)?;
    Some(&rest[chars.peek().map_or(rest.len(), |&(i, _)| i)..])
}

fn skip_shell_test(mut rest: &str) -> Option<&str> {
    loop {
        let (token, after) = shell_token(rest)?;
        rest = after;
        if token == "]]" { return Some(rest); }
    }
}

/// Skip a complete loop without inferring whether any of its body commands ran.
fn skip_shell_loop(mut rest: &str, list_loop: bool, depth: usize) -> Option<&str> {
    if depth >= 64 { return None; }
    if list_loop {
        let (variable, after) = shell_token(rest)?;
        rest = after;
        if variable == "(" && rest.starts_with('(') {
            rest = skip_shell_group(rest)?; // for ((...)); do ...; done
        } else {
            let mut name = variable.bytes();
            if !name.next().is_some_and(|c| c.is_ascii_alphabetic() || c == b'_')
                || !name.all(|c| c.is_ascii_alphanumeric() || c == b'_') { return None; }
            let (mut token, mut after) = shell_token(rest)?;
            while token == "\n" { (token, after) = shell_token(after)?; }
            match token {
                "do" => return skip_shell_commands(after, "done", depth + 1),
                "in" => {
                    rest = after;
                    loop {
                        let (token, after) = shell_token(rest)?;
                        rest = after;
                        match token {
                            ";" | "\n" => break,
                            "(" => rest = skip_shell_group(rest)?,
                            ")" | "&" | "|" => return None,
                            _ => {}
                        }
                    }
                }
                ";" => rest = after,
                _ => return None,
            }
        }
        loop {
            let (token, after) = shell_token(rest)?;
            rest = after;
            if token == "\n" || token == ";" { continue; }
            if token != "do" { return None; }
            break;
        }
    } else {
        rest = skip_shell_commands(rest, "do", depth + 1)?; // nested while/until condition
    }
    skip_shell_commands(rest, "done", depth + 1)
}

fn skip_shell_commands<'a>(mut rest: &'a str, close: &str, depth: usize) -> Option<&'a str> {
    if depth >= 64 { return None; }
    let mut command_start = true;
    let mut time_prefix = false;
    loop {
        let (token, after) = shell_token(rest)?;
        rest = after;
        match token {
            ";" | "&" | "|" | "\n" => { command_start = true; time_prefix = false; }
            // Here-document contents have their own delimiters and may contain literal done's.
            "<" if rest.starts_with('<') => return None,
            "<" | ">" if rest.starts_with('(') => { rest = skip_shell_group(&rest[1..])?; command_start = false; }
            "(" if command_start => { rest = skip_shell_group(rest)?; command_start = false; time_prefix = false; }
            // A name() function body is not an ordinary command group.
            "(" => return None,
            ")" => return None,
            _ if command_start && token == close => return Some(rest),
            "[[" if command_start => { rest = skip_shell_test(rest)?; command_start = false; }
            "for" | "select" if command_start => { rest = skip_shell_loop(rest, true, depth + 1)?; command_start = false; }
            "while" | "until" if command_start => { rest = skip_shell_loop(rest, false, depth + 1)?; command_start = false; }
            "if" if command_start => { rest = skip_shell_commands(rest, "fi", depth + 1)?; command_start = false; }
            "{" if command_start => { rest = skip_shell_commands(rest, "}", depth + 1)?; command_start = false; }
            "case" | "function" | "coproc" if command_start => return None,
            "time" if command_start => time_prefix = true,
            "-p" | "--" if command_start && time_prefix => { if token == "--" { time_prefix = false; } }
            "then" | "else" | "elif" | "do" | "!" if command_start => time_prefix = false,
            _ if command_start && SHELL_ASSIGNMENT.is_match(token) => time_prefix = false,
            // command/builtin are simple commands: their following words are command names,
            // not reserved words such as done that can close the surrounding loop.
            _ => { command_start = false; time_prefix = false; }
        }
    }
}

#[cfg(test)]
mod shell_tests {
    use super::*;

    #[test]
    fn pipeline_and_background_cds_do_not_change_the_parent() {
        for command in [
            "cd /work/beta | cat; pwd", "cat | cd /work/beta; pwd",
            "cd /work/beta |& cat; pwd", "cd /work/beta & pwd",
            "cat | cd /work/beta && pwd", "cd /work/beta | cat | pwd",
            "cat |\n cd /work/beta; pwd", "cd /work/beta && pwd & pwd",
            "cd /work/beta | cat && cd /work/gamma & pwd",
        ] {
            let (location, persistent) = shell_location(command, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/alpha"), "{command}");
            assert_eq!(persistent, location, "{command}");
        }
        for command in ["cat | cd /work/beta", "cd /work/beta &", "cd /work/beta | cat"] {
            assert_eq!(shell_location(command, Some("/work/alpha")).1.as_deref(), Some("/work/alpha"), "{command}");
        }
        assert_eq!(shell_location("cd /work/beta; cd /work/gamma | cat; cd child", Some("/work/alpha")),
            (Some("/work/beta/child".into()), Some("/work/beta/child".into())));
    }

    #[test]
    fn control_flow_does_not_claim_unexecuted_directory_changes() {
        for command in [
            "while false; do cd /work/beta; done; pwd",
            "until true; do cd /work/beta; done; pwd",
            "if false; then cd /work/beta; fi; pwd",
            "if false; then cd /work/beta; elif true; then cd /work/gamma; else cd /work/delta; fi; pwd",
            "if true; then if false; then cd /work/beta; fi; cd /work/gamma; fi; pwd",
            "while false; do if true; then cd /work/beta; fi; done; pwd",
            "if true; then while false; do cd /work/beta; done; fi; pwd",
        ] {
            let (location, persistent) = shell_location(command, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/alpha"), "{command}");
            assert_eq!(persistent, location, "{command}");
            let after = format!("{command}; cd child");
            assert_eq!(shell_location(&after, Some("/work/alpha")).1.as_deref(), Some("/work/alpha/child"), "{after}");
        }
    }

    #[test]
    fn braced_groups_preserve_the_enclosing_pipeline_and_background_scope() {
        for group in [
            "{ cd /work/beta; }",
            "{ { cd /work/beta; }; pwd; }",
            "{ echo '}'; cd /work/beta; }",
            "{ while false; do cd /work/ignored; done; cd /work/beta; }",
        ] {
            for command in [format!("{group} | cat; pwd"), format!("{group} & pwd"), format!("cat | {group}; pwd"), format!("{group} && pwd & pwd")] {
                let (location, persistent) = shell_location(&command, Some("/work/alpha"));
                assert_eq!(location.as_deref(), Some("/work/alpha"), "{command}");
                assert_eq!(persistent, location, "{command}");
            }
            let command = format!("{group}; cd child");
            let (location, persistent) = shell_location(&command, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/beta/child"), "{command}");
            assert_eq!(persistent, location, "{command}");
        }
        let command = "{ cd /work/beta; { cd /work/gamma; } | cat; pwd; }; cd child";
        assert_eq!(shell_location(command, Some("/work/alpha")).1.as_deref(), Some("/work/beta/child"));
        let deep = format!("{}cd /work/beta; {}", "{ ".repeat(128), "}; ".repeat(128));
        assert_eq!(shell_location(&deep, Some("/work/alpha")).1.as_deref(), Some("/work/alpha"));
    }

    #[test]
    fn combined_output_redirections_do_not_background_cd() {
        for redirect in ["&>/dev/null", "&>>/dev/null", "&> '/work/output with spaces'", "&> >(cat)"] {
            for command in [format!("cd /work/beta {redirect}; pwd"), format!("cd {redirect} /work/beta; pwd")] {
                let (location, persistent) = shell_location(&command, Some("/work/alpha"));
                assert_eq!(location.as_deref(), Some("/work/beta"), "{command}");
                assert_eq!(persistent, location, "{command}");
            }
            let command = format!("cd /work/beta {redirect}; cd child");
            assert_eq!(shell_location(&command, Some("/work/alpha")).1.as_deref(), Some("/work/beta/child"), "{command}");
        }
    }

    #[test]
    fn event_dedupe_keeps_newest_in_chronological_order() {
        let event = |dir: &str, strength| Event { dir: dir.into(), strength };
        let mut events = vec![event("a", Strength::Strong), event("b", Strength::Strong)];
        push_events(&mut events, vec![event("a", Strength::Weak), event("a", Strength::Strong), event("c", Strength::Strong), event("b", Strength::Strong)]);
        assert_eq!(events, vec![event("a", Strength::Weak), event("a", Strength::Strong), event("c", Strength::Strong), event("b", Strength::Strong)]);
        let many = (0..20_000).map(|i| event(&i.to_string(), Strength::Strong)).collect::<Vec<_>>();
        let mut events = many.clone();
        push_events(&mut events, many.clone());
        assert_eq!(events, many);
    }

    #[test]
    fn structured_directory_ignores_arbitrary_cwd_lines() {
        let text = "<env>\nWorking directory: /work/alpha\n</env>\n<project_instructions>\ncwd: /work/beta\n</project_instructions>";
        assert_eq!(structured_directory_in_text(text).as_deref(), Some("/work/alpha"));

        let text = "<cwd>\n/work/alpha\n</cwd>\nInstructions from: /work/alpha/AGENTS.md\ncwd: /work/beta";
        assert_eq!(structured_directory_in_text(text).as_deref(), Some("/work/alpha"));
    }

    #[test]
    fn structured_directory_supports_opencode_env_and_pi_cwd() {
        assert_eq!(
            structured_directory_in_text("<env>\n  Working directory: /work/opencode\n</env>").as_deref(),
            Some("/work/opencode")
        );
        assert_eq!(
            structured_directory_in_text("<cwd>\n/work/pi\n</cwd>").as_deref(),
            Some("/work/pi")
        );
    }

    #[test]
    fn quoted_and_escaped_directories() {
        for cmd in [
            "cd \"/work/repo with spaces\" && git status",
            "pushd '/work/repo with spaces'; git status",
            r"cd /work/repo\ with\ spaces",
            "cd /work/\"repo with\"' spaces'",
        ] {
            let (loc, persistent) = shell_location(cmd, Some("/old"));
            assert_eq!(loc.as_deref(), Some("/work/repo with spaces"), "{cmd}");
            assert_eq!(persistent, loc);
        }
        for cmd in ["git -C \"/work/repo with spaces\" status", "git -C '/work/repo with spaces' status"] {
            let (loc, persistent) = shell_location(cmd, Some("/old"));
            assert_eq!(loc.as_deref(), Some("/work/repo with spaces"));
            assert_eq!(persistent.as_deref(), Some("/old"));
        }
        assert_eq!(shell_location("cd 'relative space' && cd child", Some("/work")).0.as_deref(), Some("/work/relative space/child"));
        for cmd in ["cd \"/work/unfinished", "git -C '/work/unfinished", "cd \"$HOME/repo\""] {
            assert_eq!(shell_location(cmd, Some("/old")).0.as_deref(), Some("/old"));
        }
    }

    #[test]
    fn directory_changes_require_command_position() {
        for cmd in [
            "echo \"example: cd /work/beta && make test\"",
            "printf '%s' 'cd /work/beta'", "echo cd /work/beta",
            "sh -c 'cd /work/beta'", "echo $(cd /work/beta)",
            "echo `cd /work/beta`", "echo pushd /work/beta",
            "echo \"git -C /work/beta status\"", "echo git -C /work/beta status",
            "'if' cd /work/beta", "'{' cd /work/beta", "'!' cd /work/beta",
            "(cd /work/beta); echo done", "echo <(cd /work/beta)",
            "fn() { echo example; cd /work/beta; }",
        ] {
            let (location, persistent) = shell_location(cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/alpha"), "{cmd}");
            assert_eq!(persistent, location);
        }
        for cmd in [
            "echo 'cd /work/ignored'; cd /work/beta",
            "echo cd /work/ignored && cd /work/beta",
            "X=value cd /work/beta", "command cd /work/beta", "'cd' /work/beta",
            "echo cd > /work/ignored; cd /work/beta", "2>/dev/null cd /work/beta",
            "cd -- /work/beta", "cd /work/beta # cd /work/ignored",
            "echo <(printf example); cd /work/beta", "(cd /work/ignored); cd /work/beta",
        ] {
            let (location, persistent) = shell_location(cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
            assert_eq!(persistent, location);
        }
        let (location, persistent) = shell_location("echo 'git -C /work/ignored'; git -C /work/beta status", Some("/work/alpha"));
        assert_eq!(location.as_deref(), Some("/work/beta"));
        assert_eq!(persistent.as_deref(), Some("/work/alpha"));
    }

    #[test]
    fn timed_commands_keep_directory_tracking() {
        for prefix in ["time", "time -p", "time --", "time -p --", "! time -p"] {
            let cmd = format!("{prefix} cd /work/beta");
            let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
            assert_eq!(persistent, location);
            let cmd = format!("{prefix} git -C /work/beta status");
            let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
            assert_eq!(persistent.as_deref(), Some("/work/alpha"));
        }
        for cmd in ["echo time -p cd /work/beta", "'time' cd /work/beta", "time printf '%s' cd /work/beta", "time '-p' cd /work/beta", "time -- -p cd /work/beta"] {
            assert_eq!(shell_location(cmd, Some("/work/alpha")).0.as_deref(), Some("/work/alpha"), "{cmd}");
        }
    }

    #[test]
    fn wrapped_commands_keep_directory_tracking() {
        for prefix in [
            "command", "command -p", "command --", "command -p --",
            "builtin", "builtin --", "command '-p' '--'", "'builtin' '--'",
            "command -- builtin --", "builtin -- command -p --", "time -p command -p",
        ] {
            for name in ["cd", "pushd"] {
                let cmd = format!("{prefix} {name} /work/beta");
                let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
                assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
                assert_eq!(persistent, location);
            }
        }
        for prefix in ["command", "command -p", "command --", "command -p --", "builtin -- command -p --"] {
            let cmd = format!("{prefix} git -C /work/beta status");
            let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
            assert_eq!(persistent.as_deref(), Some("/work/alpha"));
        }
        for prefix in [
            "command -v", "command -V", "command -p -v", "command -p -V", "command -pv",
            "command -- -p", "builtin -- --", "builtin -p", "echo command -p",
            "command if", "builtin while", "command X=value",
        ] {
            let cmd = format!("{prefix} cd /work/ignored");
            let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/alpha"), "{cmd}");
            assert_eq!(persistent, location);
            let cmd = format!("{cmd}; cd child");
            assert_eq!(shell_location(&cmd, Some("/work/alpha")).0.as_deref(), Some("/work/alpha/child"), "{cmd}");
        }
    }

    #[test]
    fn run_in_folder_flags_are_strong_for_one_command() {
        for (cmd, want) in [
            ("make -C /work/beta test", "/work/beta"),
            ("make --directory=/work/beta test", "/work/beta"),
            ("npm run build --prefix ../beta", "/work/beta"),
            ("pnpm --dir /work/beta test", "/work/beta"),
            ("yarn --cwd=/work/beta test", "/work/beta"),
            ("bun --cwd /work/beta test", "/work/beta"),
            ("cargo test --manifest-path /work/beta/Cargo.toml", "/work/beta/Cargo.toml"),
            ("uv --directory /work/beta run pytest", "/work/beta"),
            ("uv run --project=/work/beta pytest", "/work/beta"),
            ("uv --directory /work/beta run --project . pytest", "/work/beta"),
            ("uv --directory=/work/beta run --project=child pytest", "/work/beta/child"),
            ("make -C /work/beta -C child test", "/work/beta/child"),
            ("go -C /work/beta test ./...", "/work/beta"),
            ("just --working-directory /work/beta test", "/work/beta"),
        ] {
            let (location, persistent) = shell_location(cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some(want), "{cmd}");
            assert_eq!(persistent.as_deref(), Some("/work/alpha"), "{cmd}");
        }
        for cmd in [
            "echo make -C /work/beta", "make test -- -C /work/beta", "npm run build --prefix",
            "npm run build -- --prefix /work/beta", "npm run build -- --prefix=/work/beta",
            "uv run -- --directory /work/beta", "cargo test -- --manifest-path=/work/beta/Cargo.toml",
        ] {
            let location = shell_location(cmd, Some("/work/alpha")).0;
            assert_eq!(location.as_deref(), Some("/work/alpha"), "{cmd}");
        }
        assert_eq!(shell_location("npm --prefix /work/beta run build -- --prefix /work/gamma", Some("/work/alpha")).0.as_deref(), Some("/work/beta"));
        assert_eq!(shell_location("npm run build -- --prefix /work/beta; cd child", Some("/work/alpha")).0.as_deref(), Some("/work/alpha/child"));
    }

    #[test]
    fn directory_flags_do_not_cross_command_boundaries() {
        for separator in [";", "&&", "||", "|", "&", "\n"] {
            for first in ["make -C /work/beta test", "uv --directory /work/beta run pytest", "git -C /work/beta status"] {
                for second in ["make -C . test", "uv --project . run pytest", "pwd"] {
                    let cmd = format!("{first} {separator} {second}");
                    let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
                    assert_eq!(location.as_deref(), Some("/work/alpha"), "{cmd}");
                    assert_eq!(persistent.as_deref(), Some("/work/alpha"), "{cmd}");
                }
            }
        }
        // A real cd changes the shell; per-command flags must use that new cwd.
        let cmd = "cd /work/beta && make -C /work/gamma test && make -C child test";
        let (location, persistent) = shell_location(cmd, Some("/work/alpha"));
        assert_eq!(location.as_deref(), Some("/work/beta/child"));
        assert_eq!(persistent.as_deref(), Some("/work/beta"));
        // Flags still chain inside a single command, and a trailing separator
        // or comment must not erase the only command's attribution.
        for suffix in ["", ";", "\n", "; # finished", "\n# finished\n"] {
            let cmd = format!("make -C /work/beta -C child test{suffix}");
            assert_eq!(shell_location(&cmd, Some("/work/alpha")).0.as_deref(), Some("/work/beta/child"), "{cmd}");
        }
    }

    #[test]
    fn complete_tests_and_loops_do_not_hide_later_commands() {
        for construct in [
            "[[ -d /work/beta ]]",
            "[[ ']] cd /work/ignored' == ']] cd /work/ignored' ]]",
            "[[ x =~ (cd|done) ]]",
            "[[ x == done # ]] cd /work/ignored\n ]]",
            "for x in a; do echo done; done",
            "for x do :; done",
            "for x\nin done do; do echo x; done",
            "select x in a; do break; done",
            "for x in a; do for y do echo done; done; done",
            "for x in a; do while true; do echo done; break; done; done",
            "for x in a; do [[ x == done ]]; done",
            "for x in a; do echo 'done; cd /work/ignored'; done",
            "for x in a; do (echo done); done",
            "for x in a; do cat < <(echo done); done",
            "for x in a; do printf x > >(cat); done",
            "for ((x=0; x<1; x++)); do :; done",
            "for x in a; do time -p for y in b; do :; done; done",
        ] {
            for separator in [";", "&&", "\n"] {
                let cmd = format!("{construct}{separator} cd /work/beta");
                let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
                assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
                assert_eq!(persistent, location);
            }
        }
        for cmd in [
            "[[ x == 'unfinished ]]; cd /work/ignored",
            "for x in a; do echo done; cd /work/ignored",
            "for x in; do cd /work/ignored; done",
            "for x in a; do echo 'done; cd /work/ignored'; done",
            "for x in a; do cat <<EOF\ndone; cd /work/ignored\nEOF\ndone",
            "for x in a; do fn() { for y in b; do :; done; cd /work/ignored; }; done",
            "for x in a; do fo\\\nr y in b; do :; done; cd /work/ignored; done",
        ] {
            assert_eq!(shell_location(cmd, Some("/work/alpha")).0.as_deref(), Some("/work/alpha"), "{cmd}");
        }
        let deep = format!("{}:{}; cd /work/ignored", "for x in a; do ".repeat(128), "; done".repeat(128));
        assert_eq!(shell_location(&deep, Some("/work/alpha")).0.as_deref(), Some("/work/alpha"));
    }

    #[test]
    fn command_names_cannot_close_loops() {
        for prefix in ["command", "builtin", "command --", "command -p", "'command'", "'builtin'", "time -p command", "! builtin"] {
            for name in ["done", "do", "for", "while", "case", "[["] {
                let cmd = format!("for x in; do {prefix} {name}; cd /work/ignored; done");
                let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
                assert_eq!(location.as_deref(), Some("/work/alpha"), "{cmd}");
                assert_eq!(persistent, location);
                let cmd = format!("{cmd}; cd child");
                let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
                assert_eq!(location.as_deref(), Some("/work/alpha/child"), "{cmd}");
                assert_eq!(persistent, location);
            }
            let cmd = format!("for x in; do while {prefix} do; do :; done; cd /work/ignored; done; cd child");
            assert_eq!(shell_location(&cmd, Some("/work/alpha")).0.as_deref(), Some("/work/alpha/child"), "{cmd}");
        }
    }

    #[test]
    fn process_substitution_redirects_do_not_hide_later_commands() {
        for cmd in [
            "printf x > >(cat); cd /work/beta",
            "cat < <(printf x); cd /work/beta",
            "printf x 2> >(cat); cd /work/beta",
            "printf x >> >(cat); cd /work/beta",
            "printf x > >(cd /work/ignored); cd /work/beta",
            "cd > >(cat) /work/beta",
        ] {
            let (location, persistent) = shell_location(cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
            assert_eq!(persistent, location);
        }
        for cmd in ["printf x > >(cd /work/ignored)", "printf x > >(cat; cd /work/ignored"] {
            assert_eq!(shell_location(cmd, Some("/work/alpha")).0.as_deref(), Some("/work/alpha"), "{cmd}");
        }
    }

    #[test]
    fn expansions_skip_only_their_argument() {
        for cmd in [
            r#"echo "$HOME"; cat /work/beta/README.md"#,
            r#"cat $HOME/unknown /work/beta/README.md"#,
            r#"cat "${HOME}/unknown path" /work/beta/README.md"#,
            r#"echo ${#HOME} ${case}; cat /work/beta/README.md"#,
            r#"echo "prefix $HOME /work/not-a-path"; cat /work/beta/README.md"#,
            r#"echo `cat /work/not-a-path`; cat /work/beta/README.md"#,
            r#"echo $(cat /work/not-a-path); cat /work/beta/README.md"#,
            r#"echo "$(cat /work/not-a-path)"; cat /work/beta/README.md"#,
            r#"echo ${HOME:-/work/not-a-path}; cat /work/beta/README.md"#,
            r#"echo $(echo "$(cat /work/not-a-path)"); cat /work/beta/README.md"#,
            r#"echo ""; cat /work/beta/README.md"#,
        ] {
            assert_eq!(shell_path_tokens(cmd), vec!["/work/beta/README.md"], "{cmd}");
        }
        // If a word never closes, there is no safe boundary at which to resume.
        assert!(shell_path_tokens(r#"echo "$HOME; cat /work/not-a-path"#).is_empty());
        assert!(shell_path_tokens("echo $(cat /work/not-a-path").is_empty());
        let nested = format!("echo {}{}; cat /work/not-a-path", "$(".repeat(128), ")".repeat(128));
        assert!(shell_path_tokens(&nested).is_empty());
    }

    #[test]
    fn comments_end_at_the_newline() {
        for cmd in [
            "# switch repos\ncd /work/beta && git status",
            "# first\n# second\ncd /work/beta",
            "echo start # ; cd /work/ignored\ncd /work/beta",
            "cd /work/beta\n# ; cd /work/ignored",
            "# $(case alpha in beta) cd /work/ignored;; esac)\ncd /work/beta",
            "# 注释 ; cd /work/ignored\ncd /work/beta",
        ] {
            assert_eq!(shell_path_tokens(cmd), vec!["/work/beta"], "{cmd}");
            let (location, persistent) = shell_location(cmd, Some("/old"));
            assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
            assert_eq!(persistent, location);
        }
        let cmd = "# git -C /work/ignored\ngit -C /work/beta status # ; cd /work/ignored";
        let (location, persistent) = shell_location(cmd, Some("/old"));
        assert_eq!(location.as_deref(), Some("/work/beta"));
        assert_eq!(persistent.as_deref(), Some("/old"));
        for cmd in ["# cd /work/ignored", "echo start # git -C /work/ignored"] {
            assert!(shell_path_tokens(cmd).is_empty());
            assert_eq!(shell_location(cmd, Some("/old")).0.as_deref(), Some("/old"));
        }
        assert_eq!(shell_location("cd # comment\n/work/other", Some("/old")).0.as_deref(), Some("/old"));
    }

    #[test]
    fn case_arguments_do_not_stop_directory_tracking() {
        for substitution in [
            "$(printf '%s' case)", "$(echo case case)", "$(echo [[)",
            "$(echo case; printf '%s' case)", "$(echo case && echo case)",
            "$(echo $(printf '%s' case))", "`printf '%s' case`",
            "$(echo 'case')", "$(echo \"case\")", "$(echo { case)",
            "$(echo <(printf '%s' case) case)", "$(echo >(cat) case)",
            "$(printf '%s' for select do)", "$(echo for; echo select)",
            "`echo for select`", "$(echo 'for' 'select')",
        ] {
            let cmd = format!("echo {substitution}; cd /work/beta");
            assert_eq!(shell_path_tokens(&cmd), vec!["/work/beta"], "{cmd}");
            let (location, persistent) = shell_location(&cmd, Some("/old"));
            assert_eq!(location.as_deref(), Some("/work/beta"), "{cmd}");
            assert_eq!(persistent, location);
        }
    }

    #[test]
    fn loop_substitutions_stop_path_extraction() {
        for header in ["for x do", "for x in a b; do", "select x do", "select x in a b; do"] {
            let body = format!("{header} case alpha in beta) cat /work/beta/README.md; cd /work/beta;; esac; done");
            for substitution in [format!("$({body})"), format!("\"$({body})\""), format!("$(echo $({body}))"), format!("`{body}`")] {
                let cmd = format!("cat /work/alpha/README.md; echo {substitution}; cat /work/after/README.md");
                assert_eq!(shell_path_tokens(&cmd), vec!["/work/alpha/README.md"], "{cmd}");
                let cmd = format!("echo {substitution}; cd /work/after");
                let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
                assert_eq!(location.as_deref(), Some("/work/alpha"), "{cmd}");
                assert_eq!(persistent, location);
            }
        }
    }

    #[test]
    fn unsupported_substitutions_stop_path_extraction() {
        for substitution in [
            "$(case alpha in beta) cat /work/beta/README.md;; esac)",
            "$(case\nalpha in beta) cat /work/beta/README.md;; esac)",
            "$(ca\\\nse alpha in beta) cat /work/beta/README.md;; esac)",
            "$(echo $(case alpha in beta) cat /work/beta/README.md;; esac))",
            "\"$(case alpha in beta) cat /work/beta/README.md;; esac)\"",
            "`case alpha in beta) cat /work/beta/README.md;; esac`",
            "$(fn() { case alpha in beta) cat /work/beta/README.md;; esac; })",
            "$(function fn { case alpha in beta) cat /work/beta/README.md;; esac; })",
            "$(coproc { case alpha in beta) cat /work/beta/README.md;; esac; })",
            "$(echo x # ) /work/beta/README.md\n)",
            "$(cat <<EOF\n) /work/beta/README.md\nEOF\n)",
            "$(cat <<-EOF\n) /work/beta/README.md\nEOF\n)",
            "$( [[ x =~ ) ]] && cat /work/beta/README.md)",
        ] {
            let cmd = format!("cat /work/alpha/README.md; echo {substitution}; cat /work/after/README.md");
            assert_eq!(shell_path_tokens(&cmd), vec!["/work/alpha/README.md"], "{cmd}");
            let cmd = format!("echo {substitution}; cd /work/after");
            let (location, persistent) = shell_location(&cmd, Some("/work/alpha"));
            assert_eq!(location.as_deref(), Some("/work/alpha"), "{cmd}");
            assert_eq!(persistent, location);
        }
        for prefix in ["true; ", "true && ", "true || ", "true | ", "true\n", "{ ", "if ", "while ", "! ", "time ", "time -p ", "for x in a; do "] {
            let cmd = format!("echo $({prefix}case alpha in beta) cat /work/beta/README.md;; esac); cd /work/after");
            assert!(shell_path_tokens(&cmd).is_empty(), "{cmd}");
            assert_eq!(shell_location(&cmd, Some("/old")).0.as_deref(), Some("/old"), "{cmd}");
        }
        // A quoted argument is not a case keyword; ordinary substitutions still resume scanning.
        for substitution in ["$(echo 'case')", "$(echo \"case\")", "$(echo showcase)"] {
            let cmd = format!("echo {substitution}; cat /work/alpha/README.md");
            assert_eq!(shell_path_tokens(&cmd), vec!["/work/alpha/README.md"], "{cmd}");
        }
    }
}

/// Directory a shell command runs in, given the directory it starts in.
/// Commands whose own flag names the directory they work in. The location is for this command only.
fn run_in_flags(command: &str) -> Option<&'static [&'static str]> {
    match command {
        "make" | "gmake" => Some(&["-C", "--directory"]),
        "npm" => Some(&["--prefix"]),
        "pnpm" => Some(&["-C", "--dir"]),
        "yarn" | "bun" => Some(&["--cwd"]),
        "cargo" => Some(&["--manifest-path"]),
        "uv" => Some(&["--directory", "--project"]),
        "go" => Some(&["-C"]),
        "just" => Some(&["-d", "--working-directory"]),
        _ => None,
    }
}

fn shell_location(cmd: &str, start: Option<&str>) -> (Option<String>, Option<String>) {
    shell_location_inner(cmd, start, 0)
}

fn shell_location_inner(cmd: &str, start: Option<&str>, depth: usize) -> (Option<String>, Option<String>) {
    // returns (location for this command, new persistent cwd after the command's cd's)
    enum Position {
        Command, CommandOptions, BuiltinOptions, WrappedCommand, Cd, Git, GitC,
        Tool(&'static [&'static str]), ToolValue(&'static [&'static str]), Argument,
    }
    let mut position = Position::Command;
    let mut time_prefix = false;
    let mut rest = paths::char_prefix(cmd, MAX_COMMAND_CHARS);
    let mut cur = start.map(String::from);
    let mut loc = cur.clone();
    let mut scope_start = cur.clone();
    let mut list_start = cur.clone();
    let mut pipeline = false;
    let mut command_boundary = false;
    loop {
        rest = rest.trim_start_matches(|c: char| c.is_whitespace() && c != '\n');
        let Some(c) = rest.chars().next() else { break };
        if c == '#' {
            rest = &rest[rest.find('\n').unwrap_or(rest.len())..];
            continue;
        }
        if ";&|\n".contains(c) && !rest.starts_with("&>") {
            // A newline after an operator continues that pipeline/and-or list.
            if c == '\n' && matches!(position, Position::Command) {
                rest = &rest[1..];
                continue;
            }
            let logical = rest.starts_with("&&") || rest.starts_with("||");
            let pipe = c == '|' && !logical;
            let background = c == '&' && !logical;
            if background {
                // The entire preceding and-or list executes asynchronously.
                cur = list_start.clone();
            } else if pipe || pipeline {
                cur = scope_start.clone();
            }
            pipeline = pipe;
            if !pipe { scope_start = cur.clone(); }
            if !pipe && !logical { list_start = cur.clone(); }
            position = Position::Command;
            time_prefix = false;
            command_boundary = true;
            rest = &rest[if logical || rest.starts_with("|&") { 2 } else { c.len_utf8() }..];
            continue;
        }
        if command_boundary {
            // Tool-local directory flags never change the shell's cwd. Reset
            // only when another command starts, so a trailing separator/comment
            // doesn't erase the last actual command's attribution.
            loc = cur.clone();
            command_boundary = false;
        }
        // Skip separate shell scopes as a unit; their cd's cannot change our persistent cwd.
        let process_substitution = rest.starts_with("<(") || rest.starts_with(">(");
        if c == '(' || process_substitution {
            if !process_substitution && !matches!(position, Position::Command) {
                break; // function definition (name() ...), not an executed subshell
            }
            let Some(after) = skip_shell_group(&rest[if process_substitution { 2 } else { 1 }..]) else { break };
            rest = after;
            position = Position::Argument;
            time_prefix = false;
            continue;
        }
        if c == ')' || rest.starts_with("<<") {
            break;
        }
        if "<>".contains(c) || rest.starts_with("&>") {
            let operator_len = if rest.starts_with("&>>") { 3 }
                else if ["&>", ">>", ">|", ">&", "<&", "<>"].iter().any(|op| rest.starts_with(op)) { 2 } else { 1 };
            rest = &rest[operator_len..];
            rest = rest.trim_start_matches(|c: char| c.is_whitespace() && c != '\n');
            if rest.starts_with("<(") || rest.starts_with(">(") {
                let Some(after) = skip_shell_group(&rest[2..]) else { break };
                rest = after;
            } else {
                let Some((_, end)) = shell_word(rest) else { break };
                if end == 0 { break; }
                rest = &rest[end..];
            }
            continue;
        }
        // Consume whole words, including quotes/substitutions, so argument contents never
        // become command names. An unsafe substitution stops this scan too.
        let Some((word, end)) = shell_word(rest) else { break };
        let raw = &rest[..end];
        rest = &rest[end..];
        if matches!(position, Position::Command) {
            if raw == "time" { time_prefix = true; continue; }
            if time_prefix && matches!(raw, "-p" | "--") {
                if raw == "--" { time_prefix = false; }
                continue;
            }
        }
        time_prefix = false;
        let reserved_words = matches!(position, Position::Command);
        position = match position {
            Position::Command if SHELL_ASSIGNMENT.is_match(raw) => Position::Command,
            // Wrapper options precede the executable name, but -- ends option parsing.
            // Lookup-only -v/-V (and unsupported options) fall through to Argument.
            Position::CommandOptions if word.as_deref() == Some("-p") => Position::CommandOptions,
            Position::CommandOptions | Position::BuiltinOptions if word.as_deref() == Some("--") => Position::WrappedCommand,
            Position::Command | Position::CommandOptions | Position::BuiltinOptions | Position::WrappedCommand => match word.as_deref() {
                Some("cd" | "pushd") => Position::Cd,
                Some("git") => Position::Git,
                Some(w) if run_in_flags(w).is_some() => Position::Tool(run_in_flags(w).unwrap()),
                Some("command") => Position::CommandOptions,
                Some("builtin") => Position::BuiltinOptions,
                Some("{") if reserved_words && raw == "{" => {
                    if depth >= 64 { break; }
                    let Some(after) = skip_shell_commands(rest, "}", 0) else { break };
                    // Analyze the body with its own separator snapshots. Its cd's
                    // affect this shell, but not the enclosing pipeline/list's
                    // saved cwd if the entire group later runs in a subshell.
                    let body = &rest[..rest.len() - after.len() - 1];
                    (loc, cur) = shell_location_inner(body, cur.as_deref(), depth + 1);
                    rest = after;
                    Position::Argument
                }
                Some("!") if reserved_words && raw == "!" => Position::Command,
                Some("if") if reserved_words && raw == "if" => {
                    let Some(after) = skip_shell_commands(rest, "fi", 0) else { break };
                    rest = after;
                    Position::Argument
                }
                Some("[[") if reserved_words && raw == "[[" => {
                    let Some(after) = skip_shell_test(rest) else { break };
                    rest = after;
                    Position::Argument
                }
                Some(w @ ("for" | "select" | "while" | "until")) if reserved_words && raw == w => {
                    let Some(after) = skip_shell_loop(rest, matches!(w, "for" | "select"), 0) else { break };
                    rest = after;
                    Position::Argument
                }
                Some(w @ ("case" | "function" | "coproc")) if reserved_words && raw == w => break,
                Some(w) if w.bytes().all(|b| b.is_ascii_digit()) && rest.starts_with(['<', '>']) => position,
                _ => Position::Argument,
            },
            Position::Cd if matches!(word.as_deref(), Some("--" | "-L" | "-P")) => Position::Cd,
            Position::Cd => {
                if let Some(t) = word.and_then(|p| abs_from(cur.as_deref(), &p)) {
                    cur = Some(t);
                    loc = cur.clone();
                }
                Position::Argument
            }
            Position::Git if word.as_deref() == Some("-C") => Position::GitC,
            Position::GitC => {
                if let Some(t) = word.and_then(|p| abs_from(cur.as_deref(), &p)) {
                    loc = Some(t);
                }
                Position::Argument
            }
            Position::Tool(flags) => {
                let Some(w) = word.as_deref() else { continue };
                if w == "--" {
                    Position::Argument
                } else if flags.contains(&w) {
                    Position::ToolValue(flags)
                } else {
                    if let Some((_, value)) = flags.iter().find_map(|f| w.strip_prefix(f).and_then(|v| v.strip_prefix('=')).map(|v| (f, v))) {
                        if let Some(t) = abs_from(loc.as_deref(), value) {
                            loc = Some(t);
                        }
                    }
                    Position::Tool(flags)
                }
            }
            Position::ToolValue(flags) => {
                if let Some(t) = word.and_then(|p| abs_from(loc.as_deref(), &p)) {
                    loc = Some(t);
                }
                Position::Tool(flags)
            }
            _ => Position::Argument,
        };
    }
    (loc, if pipeline { scope_start } else { cur })
}

/// Path-like tokens in a shell command (relative with a slash, or absolute under any root).
fn shell_path_tokens(cmd: &str) -> Vec<String> {
    let mut rest = paths::char_prefix(cmd, MAX_COMMAND_CHARS);
    let mut out: Vec<String> = Vec::new();
    loop {
        rest = rest.trim_start_matches(|c: char| c.is_whitespace() || ";&|()<>".contains(c));
        if rest.is_empty() {
            break;
        }
        if rest.starts_with('#') {
            rest = &rest[rest.find('\n').unwrap_or(rest.len())..];
            continue;
        }
        // Never extract a fragment of a quoted path: it could override a strong
        // cd event with an unrelated repository at the truncated path's prefix.
        let Some((word, end)) = shell_word(rest) else { break };
        rest = &rest[end..];
        let Some(word) = word else { continue };
        let t = word.split_once('=').map_or(word.as_str(), |(_, v)| v);
        if t.contains('/') && !t.starts_with('-') && !t.contains("://") && !out.iter().any(|o| o == t) {
            out.push(t.to_string());
            if out.len() >= 200 {
                break;
            }
        }
    }
    out
}

/// Files patched by a shell command that is exactly one Codex `apply_patch` call:
/// `apply_patch <<'EOF'` followed by a complete patch and the closing delimiter, or an argv list
/// joined with spaces (`apply_patch *** Begin Patch ...`). The patch must be the command's whole
/// input, so patch-looking text elsewhere in a script never counts. Any other shell command stays
/// weak evidence; general shell parsing is deliberately out of scope.
///
/// The whole command is scanned (not `MAX_COMMAND_CHARS`): a large patch is still one patch, and
/// the header regex is linear in its length.
fn shell_write_targets(cmd: &str) -> Vec<String> {
    let Some(patch) = sole_apply_patch(cmd.trim()) else { return Vec::new() };
    let mut paths = Vec::new();
    for m in PATCH_FILE.captures_iter(&patch) {
        if let Some(path) = m.get(1).or(m.get(2)) {
            let path = path.as_str().trim();
            if !path.is_empty() {
                paths.push(path.to_string());
            }
        }
    }
    // Keep the newest occurrence of each path, matching how attribution picks the newest event.
    let mut seen = HashSet::new();
    paths.reverse();
    paths.retain(|path| seen.insert(path.clone()));
    paths.reverse();
    paths
}

/// The patch text when `cmd` is exactly one `apply_patch` call whose input is that patch.
fn sole_apply_patch(cmd: &str) -> Option<String> {
    let program_end = cmd.find(|c: char| c.is_whitespace() || c == '<').unwrap_or(cmd.len());
    let program = cmd[..program_end].trim_matches(['\'', '"']);
    if program != "apply_patch" && !program.ends_with("/apply_patch") {
        return None;
    }
    let rest = cmd[program_end..].trim_start_matches([' ', '\t']);
    if rest.starts_with("*** Begin Patch") {
        return complete_patch(rest).map(str::to_string); // argv form: everything after the program is the patch
    }
    if rest.starts_with(['\'', '"']) {
        let (patch, end) = shell_word(rest)?;
        let patch = patch?;
        return rest[end..].trim().is_empty().then_some(()).and_then(|_| complete_patch(&patch).map(str::to_string));
    }
    let (operator, after) = rest.split_once('\n')?;
    let caps = SOLE_PATCH_HEREDOC.captures(operator.trim_end())?;
    let strip_tabs = !caps[1].is_empty();
    let delimiter = caps.get(2).or(caps.get(3)).or(caps.get(4))?.as_str();
    let is_delimiter = |line: &str| {
        let line = line.trim_end_matches('\r');
        (if strip_tabs { line.trim_start_matches('\t') } else { line }) == delimiter
    };
    // The heredoc ends at the first delimiter line, and nothing may follow it.
    let (body, last) = after.trim_end_matches(['\n', '\r']).rsplit_once('\n')?;
    if !is_delimiter(last) || body.lines().any(is_delimiter) {
        return None;
    }
    let body = if strip_tabs {
        body.lines().map(|line| line.trim_start_matches('\t')).collect::<Vec<_>>().join("\n")
    } else {
        body.to_string()
    };
    complete_patch(&body).map(str::to_string)
}

/// `patch` when its first line is exactly `*** Begin Patch` and its last is exactly `*** End Patch`.
fn complete_patch(patch: &str) -> Option<&str> {
    let patch = patch.trim_matches(['\n', '\r']);
    let first = patch.lines().next()?.trim_end();
    let last = patch.lines().last()?.trim_end();
    (first == "*** Begin Patch" && last == "*** End Patch").then_some(patch)
}

#[derive(Clone, Debug, PartialEq)]
struct Event {
    dir: String,
    strength: Strength,
}

/// Append events, keeping only the newest occurrence of each: attribution picks the newest event
/// that resolves, so an older identical event can never be chosen. Keeps a long conversation's
/// cached list as small as the set of directories it touched.
fn push_events(events: &mut Vec<Event>, new: Vec<Event>) {
    events.extend(new);
    let mut seen = HashSet::new();
    // Visit newest first, retaining only that occurrence, then restore chronology.
    events.reverse();
    events.retain(|e| seen.insert((e.dir.clone(), e.strength)));
    events.reverse();
}

fn parse_args<'d>(doc: &'d Doc, call: u32) -> Option<(Doc<'static>, bool)> {
    // returns an owned doc of the arguments object (parsed from a string when needed)
    for key in ["arguments", "input", "action", "args", "parameters"] {
        if let Some(v) = doc.get(call, key) {
            return match doc.kind(v) {
                Kind::Str => {
                    let t = doc.text(v);
                    match Doc::parse(Cow::Owned(t.as_bytes().to_vec())) {
                        Ok(d) if d.kind(d.root) == Kind::Obj => Some((d, true)),
                        // raw string input (apply_patch custom tool): wrap as {"input": "..."}
                        _ => {
                            let mut b = Vec::new();
                            b.extend_from_slice(b"{\"input\":");
                            crate::json::quote(&t, &mut b);
                            b.push(b'}');
                            Doc::parse(Cow::Owned(b)).ok().map(|d| (d, false))
                        }
                    }
                }
                Kind::Obj => Doc::parse(Cow::Owned(doc.src[doc.node(v).s as usize..doc.node(v).e as usize].to_vec())).ok().map(|d| (d, true)),
                _ => None,
            };
        }
    }
    if let Some(f) = doc.get(call, "function").filter(|&f| doc.kind(f) == Kind::Obj) {
        if let Some(a) = doc.str_of(doc.get(f, "arguments")) {
            return Doc::parse(Cow::Owned(a.as_bytes().to_vec())).ok().filter(|d| d.kind(d.root) == Kind::Obj).map(|d| (d, true));
        }
    }
    None
}

fn tool_name(doc: &Doc, call: u32) -> String {
    doc.str_of(doc.get(call, "name"))
        .or_else(|| doc.get(call, "function").and_then(|f| doc.str_of(doc.get(f, "name"))))
        .map(|s| s.into_owned())
        .unwrap_or_default()
}

fn call_id(doc: &Doc, call: u32) -> Option<String> {
    doc.str_of(doc.get(call, "id")).or_else(|| doc.str_of(doc.get(call, "call_id"))).map(|s| s.into_owned())
}

/// Tool-result text by tool_use_id (Claude Code) / call_id (Codex), for harness-reported cwd resets.
fn results_by_id<'d>(doc: &'d Doc, roots: &[u32]) -> HashMap<String, Vec<Cow<'d, str>>> {
    let mut out: HashMap<String, Vec<Cow<'d, str>>> = HashMap::new();
    fn walk<'d>(doc: &'d Doc, id: u32, out: &mut HashMap<String, Vec<Cow<'d, str>>>) {
        match doc.kind(id) {
            Kind::Obj => {
                let t = doc.str_of(doc.get(id, "type"));
                if t.as_deref() == Some("tool_result") {
                    if let (Some(k), Some(ct)) = (doc.str_of(doc.get(id, "tool_use_id")), doc.get(id, "content")) {
                        let mut texts = Vec::new();
                        content_texts(doc, ct, &mut texts);
                        out.entry(k.into_owned()).or_default().extend(texts);
                    }
                    return;
                }
                for ch in doc.children(id) {
                    walk(doc, ch, out);
                }
            }
            Kind::Arr => {
                for ch in doc.children(id) {
                    walk(doc, ch, out);
                }
            }
            _ => {}
        }
    }
    for &r in roots {
        walk(doc, r, &mut out);
    }
    out
}

/// Directory events for `calls` (document order). `shell_cwd` is Claude Code's persistent shell,
/// carried across requests in incremental mode.
fn events_over(doc: &Doc, calls: &[u32], roots: &[u32], h: Harness, stated: Option<&str>, shell_cwd: &mut Option<String>) -> Vec<Event> {
    let results = if h == Harness::ClaudeCode { results_by_id(doc, roots) } else { HashMap::new() };
    let mut out = Vec::new();
    for &call in calls {
        let name = tool_name(doc, call);
        let lname = name.to_lowercase();
        let Some((args, _)) = parse_args(doc, call) else { continue };
        let a = args.root;
        let str_arg = |keys: &[&str]| keys.iter().find_map(|k| args.str_of(args.get(a, k)).map(|s| s.into_owned()));
        let workdir = str_arg(&["workdir", "cwd", "working_directory", "directory"]).filter(|w| w.starts_with('/'));
        // action.working_directory for local_shell_call
        let base_for_call = workdir.clone().or_else(|| match h {
            Harness::ClaudeCode => shell_cwd.clone(),
            _ => stated.map(String::from),
        });
        let is_shell = matches!(lname.as_str(), "bash" | "shell" | "exec_command" | "shell_command" | "local_shell" | "container.exec" | "run_command" | "terminal")
            || doc.str_of(doc.get(call, "type")).as_deref() == Some("local_shell_call");
        let is_edit = matches!(lname.as_str(), "edit" | "write" | "multiedit" | "notebookedit" | "apply_patch" | "str_replace_based_edit_tool" | "create_file" | "write_file" | "edit_file")
            || doc.str_of(doc.get(call, "type")).as_deref() == Some("apply_patch_call");
        if is_shell {
            if let Some(cmd) = command_text(&args, a) {
                let (loc, after) = shell_location(&cmd, base_for_call.as_deref());
                if h == Harness::ClaudeCode {
                    *shell_cwd = after;
                    if let Some(id) = call_id(doc, call) {
                        for t in results.get(&id).into_iter().flatten() {
                            if let Some(m) = SHELL_RESET.captures_iter(t).last() {
                                *shell_cwd = Some(m[1].trim_end_matches('.').to_string());
                            }
                        }
                    }
                }
                let loc_dir = loc.clone();
                if let Some(l) = loc.and_then(|l| dir_of_file(&l)) {
                    out.push(Event { dir: l, strength: Strength::Strong });
                }
                // paths the command reads/writes: weak, relative to where it ran
                for tok in shell_path_tokens(&cmd) {
                    if let Some(d) = abs_from(loc_dir.as_deref(), &tok).and_then(|p| dir_of_file(&p)) {
                        out.push(Event { dir: d, strength: Strength::Weak });
                    }
                }
                // files the command writes (apply_patch, redirects, tee, sed -i): strong, like an edit tool
                for tok in shell_write_targets(&cmd) {
                    if let Some(d) = abs_from(loc_dir.as_deref(), &tok).and_then(|p| dir_of_file(&p)) {
                        out.push(Event { dir: d, strength: Strength::Strong });
                    }
                }
            }
            continue;
        }
        if is_edit {
            let mut targets: Vec<String> = Vec::new();
            if let Some(p) = str_arg(&["file_path", "filePath", "path", "notebook_path", "target_file"]) {
                targets.push(p);
            }
            if let Some(patch) = str_arg(&["input", "patch"]) {
                for m in PATCH_FILE.captures_iter(&patch) {
                    if let Some(p) = m.get(1).or(m.get(2)) {
                        targets.push(p.as_str().to_string());
                    }
                }
            }
            let base = workdir.clone().or_else(|| stated.map(String::from));
            for p in targets {
                if let Some(d) = abs_from(base.as_deref(), &p).and_then(|p| dir_of_file(&p)) {
                    out.push(Event { dir: d, strength: Strength::Strong });
                }
            }
            continue;
        }
        // reads and everything else that names a path: weak
        let base = workdir.clone().or_else(|| base_for_call.clone());
        if let Some(p) = str_arg(&["file_path", "filePath", "path", "notebook_path", "target_file", "pattern_path"]) {
            if let Some(d) = abs_from(base.as_deref(), &p).and_then(|p| dir_of_file(&p)) {
                out.push(Event { dir: d, strength: Strength::Weak });
            }
        }
    }
    out
}

fn session_key(doc: &Doc, ctx: &dyn Ctx, codex_md: Option<&Doc>) -> Option<String> {
    if let Some(s) = ctx.header("X-Claude-Code-Session-Id").filter(|s| !s.is_empty()) {
        return Some(format!("cc:{s}"));
    }
    if let Some(md) = codex_md {
        if let Some(s) = md.str_of(md.get(md.root, "session_id")) {
            return Some(format!("cx:{s}"));
        }
    }
    if let Some(s) = ctx.header("x-opencode-session-id").filter(|s| !s.is_empty()) {
        return Some(format!("oc:{s}"));
    }
    if let Some(s) = ctx.header("x-session-id").filter(|s| !s.is_empty()) {
        return Some(format!("sid:{s}"));
    }
    let _ = doc;
    None
}

/// Memory key for one Codex thread. A thread's `session_id` can differ from its `thread_id`, and
/// children name their parent by thread, so thread memory has its own namespace.
fn thread_key(codex_md: Option<&Doc>) -> Option<String> {
    let md = codex_md?;
    let t = md.str_of(md.get(md.root, "thread_id")).or_else(|| md.str_of(md.get(md.root, "session_id"))).filter(|t| !t.is_empty())?;
    Some(format!("thread:{t}"))
}

/// Memory keys for the parent of a Codex child thread (a guardian review or a spawned agent),
/// most specific first: the exact parent turn, then the parent thread.
fn parent_memory_keys(ctx: &dyn Ctx, codex_md: Option<&Doc>) -> Vec<String> {
    let mut keys = Vec::new();
    if let Some(md) = codex_md {
        if let Some(t) = md.str_of(md.get(md.root, "parent_turn_id")).filter(|t| !t.is_empty()) {
            keys.push(format!("turn:{t}"));
        }
        if let Some(t) = md.str_of(md.get(md.root, "parent_thread_id")).filter(|t| !t.is_empty()) {
            keys.push(format!("thread:{t}"));
        }
    }
    if let Some(t) = ctx.header("x-codex-parent-thread-id").filter(|t| !t.is_empty()) {
        let k = format!("thread:{t}");
        if !keys.contains(&k) {
            keys.push(k);
        }
    }
    keys
}

fn basename_of(d: &str) -> String {
    paths::basename(paths::rstrip_chars(d, "/")).to_string()
}

fn set_repo(a: &mut Attribution, r: &Resolved, source: &str, dsrc: &str, dir: &str) {
    a.repository = Some(r.slug.clone());
    a.repository_host = r.host.clone();
    a.repository_source = Some(source.to_string());
    a.directory_source = Some(dsrc.to_string());
    a.directory_basename = Some(basename_of(dir));
}

fn explicit_of(doc: &Doc, ctx: &dyn Ctx) -> Option<String> {
    let mut explicit = ctx.header("X-Router-Repository").filter(|v| !v.is_empty());
    if explicit.is_none() && doc.kind(doc.root) == Kind::Obj {
        if let Some(m) = doc.get(doc.root, "metadata").filter(|&m| doc.kind(m) == Kind::Obj) {
            if let Some(r) = doc.str_of(doc.get(m, "repository")).filter(|r| !r.is_empty()) {
                explicit = Some(r.into_owned());
            }
        }
    }
    explicit
}

fn harness_of(ctx: &dyn Ctx, codex_md: Option<&Doc>) -> Harness {
    // Codex identifies itself with x-codex-turn-metadata; other Responses-API clients (Pi) are generic.
    if codex_md.is_some() {
        Harness::Codex
    } else if ctx.header("X-Claude-Code-Session-Id").is_some() || ctx.header("x-gateway-client").is_some_and(|v| v == "claude-code") {
        // Persistent-shell semantics are Claude Code's alone; Pi and OpenCode also send a `system`
        // field but run each command in a fresh shell (verified by where their edits land).
        Harness::ClaudeCode
    } else {
        Harness::Generic
    }
}

/// One conversation (a session's main agent, a subagent, or a Codex thread).
fn conv_cache_key(ctx: &dyn Ctx, codex_md: Option<&Doc>) -> Option<String> {
    if let Some(s) = ctx.header("X-Claude-Code-Session-Id").filter(|s| !s.is_empty()) {
        let agent = ctx.header("x-claude-code-agent-id").unwrap_or_default();
        return Some(format!("cc:{s}:{agent}"));
    }
    if let Some(md) = codex_md {
        let t = md.str_of(md.get(md.root, "thread_id")).or_else(|| md.str_of(md.get(md.root, "session_id")))?;
        return Some(format!("cx:{t}"));
    }
    if let Some(s) = ctx.header("x-opencode-session-id").filter(|s| !s.is_empty()) {
        return Some(format!("oc:{s}"));
    }
    ctx.header("x-session-id").filter(|s| !s.is_empty()).map(|s| format!("sid:{s}"))
}

/// What a request's conversation contributes to attribution.
struct Analysis {
    stated: Option<Stated>,
    events: Vec<Event>,
    shell_end: Option<String>,
    msg_dir: Option<String>,
    conv_key: Option<&'static str>,
    top: Option<Stated>,
    top_fp: u64,
    last_call_id: Option<String>,
    layout: Option<Layout>,
    user_turns: UserTurns,
    boundary_user_turns: UserTurns,
    harness: Harness,
}

/// Where the conversation sits in the body, so the next request can check cheaply that everything
/// before its new messages is unchanged.
#[derive(Clone, Debug)]
struct Layout {
    /// Offset of the `,` (or `{`) before the conversation member's key; members before it are the prefix.
    prefix_end: usize,
    prefix_fp: u64,
    /// The prefix has members the attribution or the splice needs (metadata, or a system prompt
    /// that also appears after the conversation): parse it instead of trusting the cache.
    prefix_parse: bool,
    /// End offset of the conversation element holding the newest tool call. Unchanged prefix and
    /// history put it at the same offset in the next request.
    boundary_end: usize,
}

fn fp_bytes(raw: &[u8], mut h: u64) -> u64 {
    h = (h ^ raw.len() as u64).wrapping_mul(0xff51_afd7_ed55_8ccd);
    let mut chunks = raw.chunks_exact(8);
    for c in &mut chunks {
        let w = u64::from_le_bytes(c.try_into().unwrap());
        h = (h ^ w).wrapping_mul(0xc4ce_b9fe_1a85_ec53).rotate_left(29);
    }
    for &b in chunks.remainder() {
        h = (h ^ b as u64).wrapping_mul(0x1000_0000_01b3);
    }
    h
}

/// The byte before a member's key: `,` or `{` (offset), skipping whitespace.
fn before_key(src: &[u8], key_s: usize) -> Option<usize> {
    let mut i = key_s.checked_sub(1)?; // the opening quote
    while i > 0 {
        i -= 1;
        match src[i] {
            b' ' | b'\t' | b'\n' | b'\r' => continue,
            b',' | b'{' => return Some(i),
            _ => return None,
        }
    }
    None
}

/// Claude Code tool results change how a call is read (shell resets), so a conversation is only
/// resumable after a message whose calls all have their results later in the body.
fn results_follow(doc: &Doc, h: Harness, elem: u32, after: usize) -> bool {
    if h != Harness::ClaudeCode {
        return true;
    }
    let mut calls = Vec::new();
    collect_tool_calls(doc, elem, &mut calls);
    let rest = &doc.src[after..];
    calls.iter().filter_map(|&k| call_id(doc, k)).all(|id| memchr::memmem::find(rest, format!("\"tool_use_id\":\"{id}\"").as_bytes()).is_some())
}

fn layout_of(doc: &Doc, h: Harness, conv_key: &str, calls: &[u32]) -> Option<Layout> {
    use crate::json::NONE;
    let root = doc.root;
    let conv = doc.get(root, conv_key).filter(|&a| doc.kind(a) == Kind::Arr)?;
    let cn = doc.node(conv);
    // every tool call, and every tool result, must be inside the conversation; otherwise the cached
    // events would depend on members that are re-read (or not) differently next time
    if calls.iter().any(|&k| doc.node(k).s < cn.s || doc.node(k).e > cn.e) {
        return None;
    }
    let mut prefix_has = (false, false);
    let mut tail_top = false;
    for m in doc.children(root) {
        if m == conv {
            continue;
        }
        let k = doc.key_text(m);
        let n = doc.node(m);
        if may_hold_calls(&doc.src[n.s as usize..n.e as usize]) && !results_by_id(doc, &[m]).is_empty() {
            return None;
        }
        let before = n.s < cn.s;
        match k.as_ref() {
            "metadata" if before => prefix_has.0 = true,
            "system" | "instructions" if before => prefix_has.1 = true,
            "system" | "instructions" => tail_top = true,
            _ => {}
        }
    }
    if cn.key_s == NONE {
        return None;
    }
    let prefix_end = before_key(&doc.src, cn.key_s as usize)?;
    let last = *calls.last()?;
    let ln = doc.node(last);
    let elem = doc.children(conv).find(|&e| doc.node(e).s <= ln.s && doc.node(e).e >= ln.e)?;
    if !results_follow(doc, h, elem, doc.node(elem).e as usize) {
        return None;
    }
    Some(Layout {
        prefix_end,
        prefix_fp: fp_bytes(&doc.src[..prefix_end], 7),
        prefix_parse: prefix_has.0 || (prefix_has.1 && tail_top),
        boundary_end: doc.node(elem).e as usize,
    })
}

fn analyze_full(doc: &Doc, h: Harness, codex_md: Option<&Doc>) -> Analysis {
    let (conv_key, msg_dir) = msg_stated_full(doc, h);
    let top = top_stated(doc, h);
    let top_fp = top_fingerprint(doc);
    let stated = combine(h, msg_dir.clone(), top.clone(), codex_md);
    let mut calls = Vec::new();
    collect_tool_calls(doc, doc.root, &mut calls);
    let mut shell = stated.as_ref().map(|s| s.dir.clone());
    let mut events = Vec::new();
    push_events(&mut events, events_over(doc, &calls, &[doc.root], h, stated.as_ref().map(|s| s.dir.as_str()), &mut shell));
    let last_call_id = calls.iter().rev().find_map(|&c| call_id(doc, c));
    let with_id: Vec<u32> = calls.iter().copied().filter(|&c| call_id(doc, c).is_some()).collect();
    let layout = conv_key.and_then(|k| layout_of(doc, h, k, &with_id));
    let (user_turns, boundary_user_turns) = user_turns_full(doc, h, layout.as_ref().map_or(usize::MAX, |l| l.boundary_end));
    Analysis { stated, events, shell_end: shell, msg_dir, conv_key, top, top_fp, last_call_id, layout, user_turns, boundary_user_turns, harness: h }
}

/// Resume from the state cached after the previous request: only messages after the boundary are read.
/// None when anything differs from what the full analysis would assume (the caller then does it fully).
fn analyze_incremental(doc: &Doc, new_elems: &[u32], h: Harness, codex_md: Option<&Doc>, cached: &ConvState) -> Option<Analysis> {
    if cached.harness != h {
        return None;
    }
    let msg_dir = msg_stated(doc, new_elems, h).or_else(|| cached.msg_dir.clone());
    let has_top = doc.get(doc.root, "system").is_some() || doc.get(doc.root, "instructions").is_some();
    let top_fp = if has_top { top_fingerprint(doc) } else { cached.top_fp };
    let top = if !has_top || top_fp == cached.top_fp { cached.top.clone() } else { top_stated(doc, h) };
    let stated = combine(h, msg_dir.clone(), top.clone(), codex_md);
    if stated.as_ref().map(|s| &s.dir) != cached.stated_dir.as_ref() {
        return None; // event bases would differ from the cached ones
    }
    let mut calls = Vec::new();
    for &e in new_elems {
        collect_tool_calls(doc, e, &mut calls);
    }
    let mut shell = cached.shell_end.clone();
    let new_events = events_over(doc, &calls, new_elems, h, stated.as_ref().map(|s| s.dir.as_str()), &mut shell);
    let mut events = cached.events.clone();
    push_events(&mut events, new_events);
    let newest = calls.iter().rev().find(|&&c| call_id(doc, c).is_some()).copied();
    let last_call_id = newest.and_then(|c| call_id(doc, c)).or_else(|| Some(cached.last_call_id.clone()));
    let mut layout = cached.layout.clone();
    if let Some(c) = newest {
        let ln = doc.node(c);
        let elem = *new_elems.iter().find(|&&e| doc.node(e).s <= ln.s && doc.node(e).e >= ln.e)?;
        layout.boundary_end = doc.node(elem).e as usize;
        if !results_follow(doc, h, elem, layout.boundary_end) {
            return None;
        }
    }
    let (user_turns, boundary_user_turns) = user_turns(doc, new_elems, cached.user_turns, layout.boundary_end);
    Some(Analysis { stated, events, shell_end: shell, msg_dir, conv_key: Some(cached.conv_key), top, top_fp, last_call_id, layout: Some(layout), user_turns, boundary_user_turns, harness: h })
}

fn finish(doc: &Doc, ctx: &mut dyn Ctx, codex_md: Option<&Doc>, an: &Analysis) -> R<Attribution> {
    let mut a = Attribution::default();
    (a.turn_id, a.turn_id_source) = turn_identity(doc, ctx, codex_md, an.user_turns);
    a.harness = Some(match an.harness { Harness::Codex => "codex", Harness::ClaudeCode => "claude_code", Harness::Generic => "generic" }.into());
    let resolver: &RepoResolver = ctx.resolver();
    let stated = &an.stated;
    let stated_res = match stated {
        Some(s) => resolver.resolve(&s.dir),
        None => None,
    };
    if let Some(s) = stated {
        a.directory_basename = Some(basename_of(&s.dir));
        a.directory_source = Some(s.label.clone());
    }
    if let Some(r) = &stated_res {
        a.cwd_repository = Some(r.slug.clone());
    }
    let evs = &an.events;
    let skey = session_key(doc, ctx, codex_md);
    let tkey = a.turn_id.as_ref().map(|t| format!("turn:{t}"));
    let thkey = thread_key(codex_md);
    // Remember a resolved repository for this turn and thread, so child threads can inherit it.
    let remember_turn = |ctx: &dyn Ctx, r: &Resolved, d: &str| {
        for k in [&tkey, &thkey].into_iter().flatten() {
            ctx.session_put(k, (r.clone(), d.to_string()));
        }
    };
    // ... and for the session, when this request's own evidence resolved it.
    let remember = |ctx: &dyn Ctx, r: &Resolved, d: &str| {
        if let Some(k) = &skey {
            ctx.session_put(k, (r.clone(), d.to_string()));
        }
        remember_turn(ctx, r, d);
    };

    // newest event (of the allowed strengths) that resolves to a repository
    let pick = |allow_weak: bool| -> R<Option<(Resolved, String, Strength)>> {
        for e in evs.iter().rev() {
            if e.strength == Strength::Weak && !allow_weak {
                continue;
            }
            if let Some(r) = resolver.resolve(&e.dir) {
                return Ok(Some((r, e.dir.clone(), e.strength)));
            }
        }
        Ok(None)
    };
    let chosen: Option<(Resolved, String, &str, Strength)> = if let Some(sr) = &stated_res {
        match pick(false)? {
            Some((r, d, strength)) => Some((r, d, "tool_call", strength)),
            None => Some((sr.clone(), stated.as_ref().unwrap().dir.clone(), "stated", Strength::Strong)),
        }
    } else {
        pick(true)?.map(|(r, d, strength)| (r, d, "tool_call", strength))
    };
    if let Some((r, d, how, strength)) = chosen {
        if how == "stated" {
            let label = stated.as_ref().unwrap().label.clone();
            set_repo(&mut a, &r, r.source, &label, &d);
        } else {
            let src = if stated_res.as_ref().is_some_and(|s| s.slug == r.slug) {
                r.source
            } else if strength == Strength::Weak {
                "tool_read_path"
            } else {
                "tool_call_path"
            };
            set_repo(&mut a, &r, src, "tool_call", &d);
        }
        remember(ctx, &r, &d);
        return Ok(a);
    }
    // Nothing in this request resolved a repository.
    // A Codex child thread works for its parent turn. It often runs from a shared folder such as
    // ~/code, and a guardian may run a probe like `ps` that stays in its history, which disables
    // the session-memory rule below for the rest of the thread. Main threads keep that rule;
    // only explicit child threads inherit the parent's repository, and the child's own evidence
    // (handled above) always wins. The exact parent turn is current, so it beats the child's own
    // session memory, which may come from an earlier task of a reused child thread.
    let parents = parent_memory_keys(ctx, codex_md);
    let inherit = |a: &mut Attribution, ctx: &dyn Ctx, k: &str| -> bool {
        let Some((r, d)) = ctx.session_get(k) else { return false };
        let src = if k.starts_with("turn:") { "parent_turn_memory" } else { "parent_session_memory" };
        set_repo(a, &r, src, "parent_memory", &d);
        // so this child's own children (which name this turn or thread) inherit it too
        remember_turn(ctx, &r, &d);
        true
    };
    if let Some(k) = parents.iter().find(|k| k.starts_with("turn:")) {
        if inherit(&mut a, ctx, k) {
            return Ok(a);
        }
    }
    if evs.is_empty() {
        if let Some((r, d)) = skey.as_ref().and_then(|k| ctx.session_get(k)) {
            set_repo(&mut a, &r, if r.source == "git_remote" { "git_remote" } else { r.source }, "session_memory", &d);
            remember_turn(ctx, &r, &d);
            return Ok(a);
        }
    }
    for k in parents.iter().filter(|k| !k.starts_with("turn:")) {
        if inherit(&mut a, ctx, k) {
            return Ok(a);
        }
    }
    if stated.is_none() {
        if let Some(cwd) = ctx.calling_process(&mut a) {
            if let Some(r) = resolver.resolve(&cwd) {
                set_repo(&mut a, &r, r.source, "client_process_cwd", &cwd);
                remember(ctx, &r, &cwd);
                return Ok(a);
            }
        }
    }
    a.error = Some(if stated.is_none() { "no_directory" } else { "not_a_git_repository" }.into());
    Ok(a)
}

fn explicit_attr(e: String) -> Attribution {
    Attribution { repository: Some(e), repository_source: Some("explicit".into()), directory_source: Some("explicit".into()), ..Default::default() }
}

pub fn plan(doc: &Doc, a: &Attribution) -> Option<Plan> {
    let mut fields = Vec::new();
    if let Some(repo) = a.repository.as_deref().filter(|r| !r.is_empty()) {
        if a.repository_source.as_deref() != Some("explicit") {
            fields.push(("repository", repo));
            fields.push(("repository_source", a.repository_source.as_deref().unwrap_or("")));
        }
    }
    if let Some(turn_id) = a.turn_id.as_deref() {
        fields.push(("turn_id", turn_id));
        fields.push(("turn_id_source", a.turn_id_source.as_deref().unwrap_or("")));
    }
    crate::json::splice_fields(doc, &fields)
}

// ------------------------------------------------------------- incremental parsing across requests

/// State after a request, enough to resume the next request of the same conversation.
#[derive(Clone, Debug)]
pub struct ConvState {
    last_call_id: String,
    events: Vec<Event>,
    shell_end: Option<String>,
    msg_dir: Option<String>,
    conv_key: &'static str,
    top: Option<Stated>,
    top_fp: u64,
    stated_dir: Option<String>,
    harness: Harness,
    layout: Layout,
    user_turns: UserTurns,
    /// Requests attributed incrementally since the last full parse.
    run: u32,
}

/// A conversation is re-parsed in full at least this often, which bounds how long an in-place
/// rewrite of old history (same length, invisible to the boundary checks) can go unnoticed.
const FULL_EVERY: u32 = 16;

/// Up to four conversation states per key (main agent and side conversations share session ids).
pub struct ConvCache {
    map: Mutex<HashMap<String, Vec<(Instant, ConvState)>>>,
}

impl Default for ConvCache {
    fn default() -> Self {
        Self { map: Mutex::new(HashMap::new()) }
    }
}

impl ConvCache {
    const TTL: Duration = Duration::from_secs(6 * 3600);
    fn get(&self, key: &str) -> Vec<ConvState> {
        let m = self.map.lock().unwrap();
        m.get(key).map(|v| v.iter().filter(|(t, _)| t.elapsed() < Self::TTL).map(|(_, s)| s.clone()).collect()).unwrap_or_default()
    }
    fn put(&self, key: &str, replaced: Option<&str>, st: ConvState) {
        let mut m = self.map.lock().unwrap();
        if m.len() >= 4096 && !m.contains_key(key) {
            let cutoff = Instant::now() - Self::TTL;
            m.retain(|_, v| v.iter().any(|(t, _)| *t > cutoff));
            if m.len() >= 4096 {
                m.clear();
            }
        }
        let v = m.entry(key.to_string()).or_default();
        v.retain(|(_, s)| Some(s.last_call_id.as_str()) != replaced && s.last_call_id != st.last_call_id);
        v.insert(0, (Instant::now(), st));
        v.truncate(4);
    }
}

pub struct FixedResult {
    pub attribution: Attribution,
    pub plan: Option<Plan>,
    /// The request was attributed from the cached state plus its new messages only.
    pub incremental: bool,
    /// Verify mode: Some(description) when the incremental result differs from a full parse.
    pub mismatch: Option<String>,
}

/// The tail of a request: the members after the conversation array, and the conversation's messages
/// after the boundary (the message holding the cached newest tool call).
struct Tail<'a> {
    doc: Doc<'a>,
    new_elems: Vec<u32>,
    matched: usize,
}

enum Walk<'a> {
    /// The conversation continues a cached state; only the tail was parsed.
    Tail(Tail<'a>),
    /// No cached boundary in this body (a side conversation sharing the key): the walk parsed the
    /// whole document, which is returned instead of being parsed again.
    Whole(Doc<'a>),
}

fn tail_walk<'a>(body: &'a [u8], states: &[ConvState]) -> Option<Walk<'a>> {
    use crate::json::{RParser, NONE as N};
    let mut doc = Doc { src: Cow::Borrowed(body), nodes: Vec::new(), root: N };
    let rp_ws = |e: usize| -> usize {
        let mut e = e;
        while e > 0 && matches!(body[e - 1], b' ' | b'\t' | b'\n' | b'\r') {
            e -= 1;
        }
        e
    };
    let root_end = rp_ws(body.len());
    if root_end == 0 || body[root_end - 1] != b'}' {
        return None;
    }
    let mut members_rev: Vec<u32> = Vec::new();
    let mut pos = rp_ws(root_end - 1);
    loop {
        if pos == 0 {
            return None;
        }
        if body[pos - 1] == b'{' {
            if rp_ws(pos - 1) != 0 {
                return None;
            }
            let root = push_container(&mut doc, Kind::Obj, pos - 1, root_end);
            RParser { b: body, nodes: &mut doc.nodes }.link(root, &members_rev);
            doc.root = root;
            return Some(Walk::Whole(doc));
        }
        if body[pos - 1] == b']' {
            // an array member: walk its elements from the end, looking for the boundary
            let arr_end = pos;
            let mut epos = rp_ws(pos - 1);
            let mut elems_rev: Vec<u32> = Vec::new();
            let mut found: Option<usize> = None;
            let mut bend = 0;
            loop {
                if epos == 0 {
                    return None;
                }
                if body[epos - 1] == b'[' {
                    break;
                }
                bend = epos;
                let (eid, es) = RParser { b: body, nodes: &mut doc.nodes }.value(epos, 2).ok()?;
                // only an element that can hold a cached id is examined (ids are matched after decoding,
                // so an element with a \u escape is examined too)
                let raw = &body[es..epos];
                if states.iter().any(|s| memchr::memmem::find(raw, s.last_call_id.as_bytes()).is_some()) || memchr::memmem::find(raw, b"\\u").is_some() {
                    let mut calls = Vec::new();
                    collect_tool_calls(&doc, eid, &mut calls);
                    let ids: Vec<String> = calls.iter().filter_map(|&k| call_id(&doc, k)).collect();
                    if let Some(i) = states.iter().position(|s| ids.iter().any(|x| *x == s.last_call_id)) {
                        found = Some(i);
                        break;
                    }
                }
                elems_rev.push(eid);
                epos = rp_ws(es);
                if epos == 0 {
                    return None;
                }
                match body[epos - 1] {
                    b',' => epos = rp_ws(epos - 1),
                    b'[' => {}
                    _ => return None,
                }
            }
            if let Some(i) = found {
                // this is the conversation: everything before it is already in the cached state, which
                // holds only if the prefix is byte-identical and the history ends where it did
                let st = &states[i];
                let lay = &st.layout;
                if bend != lay.boundary_end || lay.prefix_end >= bend || !matches!(body[lay.prefix_end], b',' | b'{') {
                    return None;
                }
                let mut q = lay.prefix_end + 1;
                let ws = |q: &mut usize| {
                    while *q < body.len() && matches!(body[*q], b' ' | b'\t' | b'\n' | b'\r') {
                        *q += 1;
                    }
                };
                ws(&mut q);
                let key = st.conv_key.as_bytes();
                if body.get(q) != Some(&b'"') || body.get(q + 1..q + 1 + key.len()) != Some(key) || body.get(q + 1 + key.len()) != Some(&b'"') {
                    return None;
                }
                q += key.len() + 2;
                ws(&mut q);
                if body.get(q) != Some(&b':') {
                    return None;
                }
                q += 1;
                ws(&mut q);
                if body.get(q) != Some(&b'[') || fp_bytes(&body[..lay.prefix_end], 7) != lay.prefix_fp {
                    return None;
                }
                for &m in &members_rev {
                    let n = doc.node(m);
                    if !may_hold_calls(&body[n.s as usize..n.e as usize]) {
                        continue;
                    }
                    let mut calls = Vec::new();
                    collect_tool_calls(&doc, m, &mut calls);
                    if !calls.is_empty() || !results_by_id(&doc, &[m]).is_empty() {
                        return None;
                    }
                }
                let root = push_container(&mut doc, Kind::Obj, 0, root_end);
                let mut kids_rev = members_rev.clone();
                if lay.prefix_parse && lay.prefix_end > 0 {
                    // small prefix (model, metadata, ...): parse it forward and add its members
                    let mut pre = body[..lay.prefix_end].to_vec();
                    pre.push(b'}');
                    let pdoc = Doc::parse(Cow::Owned(pre)).ok()?;
                    let base = doc.nodes.len() as u32;
                    for n in &pdoc.nodes {
                        let mut n = n.clone();
                        n.first = if n.first == crate::json::NONE { n.first } else { n.first + base };
                        n.next = if n.next == crate::json::NONE { n.next } else { n.next + base };
                        doc.nodes.push(n);
                    }
                    let pk: Vec<u32> = pdoc.children(pdoc.root).map(|k| k + base).collect();
                    kids_rev.extend(pk.into_iter().rev());
                }
                RParser { b: body, nodes: &mut doc.nodes }.link(root, &kids_rev);
                doc.root = root;
                elems_rev.reverse();
                return Some(Walk::Tail(Tail { doc, new_elems: elems_rev, matched: i }));
            }
            // a complete array that is not the conversation (tools, include, ...): it is a member value
            let start = epos - 1;
            let arr = push_container(&mut doc, Kind::Arr, start, arr_end);
            RParser { b: body, nodes: &mut doc.nodes }.link(arr, &elems_rev);
            pos = finish_member(body, &mut doc, arr, start, &rp_ws, &mut members_rev)?;
            continue;
        }
        let (vid, vs) = RParser { b: body, nodes: &mut doc.nodes }.value(pos, 1).ok()?;
        pos = finish_member(body, &mut doc, vid, vs, &rp_ws, &mut members_rev)?;
    }
}

/// Raw bytes can only decode to a tool call or result if one of these appears literally (escapes
/// other than \u cannot produce letters).
fn may_hold_calls(raw: &[u8]) -> bool {
    use std::sync::OnceLock;
    static AC: OnceLock<aho_corasick::AhoCorasick> = OnceLock::new();
    let ac = AC.get_or_init(|| {
        let mut pats: Vec<String> = TOOL_CALL_TYPES.iter().map(|t| format!("\"{t}\"")).collect();
        pats.extend(["\"function\"".to_string(), "\"tool_result\"".to_string(), "\\u".to_string()]);
        aho_corasick::AhoCorasick::new(pats).expect("patterns")
    });
    ac.is_match(raw)
}

fn push_container(doc: &mut Doc, kind: Kind, s: usize, e: usize) -> u32 {
    use crate::json::NONE as N;
    doc.nodes.push(crate::json::Node { kind, s: s as u32, e: e as u32, esc: false, key_s: N, key_e: N, key_esc: false, first: N, next: N });
    (doc.nodes.len() - 1) as u32
}

/// Read the key before a top-level member value; returns the position before the member's separator.
fn finish_member(body: &[u8], doc: &mut Doc, vid: u32, vs: usize, rp_ws: &dyn Fn(usize) -> usize, members_rev: &mut Vec<u32>) -> Option<usize> {
    let mut pos = rp_ws(vs);
    if pos == 0 || body[pos - 1] != b':' {
        return None;
    }
    pos = rp_ws(pos - 1);
    if pos == 0 || body[pos - 1] != b'"' {
        return None;
    }
    // key strings are short; find the opening quote
    let close = pos - 1;
    let mut j = close;
    let open = loop {
        let k = memchr::memrchr(b'"', &body[..j])?;
        let mut bs = 0;
        while k > bs && body[k - 1 - bs] == b'\\' {
            bs += 1;
        }
        if bs % 2 == 0 {
            break k;
        }
        j = k;
    };
    let n = &mut doc.nodes[vid as usize];
    n.key_s = (open + 1) as u32;
    n.key_e = close as u32;
    n.key_esc = true;
    members_rev.push(vid);
    pos = rp_ws(open);
    if pos == 0 {
        return None;
    }
    match body[pos - 1] {
        b',' => Some(rp_ws(pos - 1)),
        b'{' => Some(pos),
        _ => None,
    }
}

fn state_from(an: &Analysis, h: Harness, run: u32) -> Option<ConvState> {
    Some(ConvState {
        last_call_id: an.last_call_id.clone()?,
        events: an.events.clone(),
        shell_end: an.shell_end.clone(),
        msg_dir: an.msg_dir.clone(),
        conv_key: an.conv_key?,
        top: an.top.clone(),
        top_fp: an.top_fp,
        stated_dir: an.stated.as_ref().map(|s| s.dir.clone()),
        harness: h,
        layout: an.layout.clone()?,
        user_turns: an.boundary_user_turns,
        run,
    })
}

fn full_bytes(body: &[u8], ctx: &mut dyn Ctx) -> R<(Attribution, Option<Plan>, Option<(Analysis, Harness)>)> {
    let doc = Doc::parse(Cow::Borrowed(body)).map_err(|e| e.name())?;
    full_doc(&doc, ctx)
}

fn full_doc(doc: &Doc, ctx: &mut dyn Ctx) -> R<(Attribution, Option<Plan>, Option<(Analysis, Harness)>)> {
    if let Some(e) = explicit_of(doc, ctx) {
        return Ok((explicit_attr(e), None, None));
    }
    if doc.kind(doc.root) != Kind::Obj {
        return Ok((Attribution { error: Some("no_directory".into()), ..Default::default() }, None, None));
    }
    let codex_md = header_json(ctx, "x-codex-turn-metadata");
    let h = harness_of(ctx, codex_md.as_ref());
    let an = analyze_full(doc, h, codex_md.as_ref());
    let a = finish(doc, ctx, codex_md.as_ref(), &an)?;
    let p = plan(doc, &a);
    Ok((a, p, Some((an, h))))
}

/// Fixed-mode attribution of one request body, incremental when the conversation is cached.
pub fn attribute_bytes(body: &[u8], ctx: &mut dyn Ctx, cache: &ConvCache, verify: bool) -> R<FixedResult> {
    let codex_md = header_json(ctx, "x-codex-turn-metadata");
    let h = harness_of(ctx, codex_md.as_ref());
    let ckey = conv_cache_key(ctx, codex_md.as_ref());
    if let Some(k) = &ckey {
        let states: Vec<ConvState> = cache.get(k).into_iter().filter(|s| s.run + 1 < FULL_EVERY).collect();
        if !states.is_empty() {
            let (walked, whole) = match tail_walk(body, &states) {
                Some(Walk::Tail(t)) => (Some(t), None),
                Some(Walk::Whole(d)) => (None, Some(d)),
                None => (None, None),
            };
            if let Some(d) = whole {
                // side conversation: attribute the document the walk already built
                let (a, p, an) = full_doc(&d, ctx)?;
                if let Some((an, h)) = an {
                    if let Some(st) = state_from(&an, h, 0) {
                        cache.put(k, None, st);
                    }
                }
                let mismatch = if verify {
                    let (fa, fp, _) = full_bytes(body, ctx)?;
                    (fa != a || fp != p).then(|| format!("whole-walk {:?} vs full {:?}", a, fa))
                } else {
                    None
                };
                return Ok(FixedResult { attribution: a, plan: p, incremental: false, mismatch });
            }
            if let Some(t) = walked {
                if let Some(e) = explicit_of(&t.doc, ctx) {
                    return Ok(FixedResult { attribution: explicit_attr(e), plan: None, incremental: true, mismatch: None });
                }
                let cached = &states[t.matched];
                if let Some(an) = analyze_incremental(&t.doc, &t.new_elems, h, codex_md.as_ref(), cached) {
                    let a = finish(&t.doc, ctx, codex_md.as_ref(), &an)?;
                    let p = plan(&t.doc, &a);
                    if let Some(st) = state_from(&an, h, cached.run + 1) {
                        cache.put(k, Some(&cached.last_call_id), st);
                    }
                    let mismatch = if verify {
                        let (fa, fp, _) = full_bytes(body, ctx)?;
                        let ib = p.as_ref().map(|p| crate::json::apply_plan(body, p));
                        let fb = fp.as_ref().map(|p| crate::json::apply_plan(body, p));
                        (fa != a || ib != fb).then(|| format!("incremental {:?} vs full {:?}", a, fa))
                    } else {
                        None
                    };
                    return Ok(FixedResult { attribution: a, plan: p, incremental: true, mismatch });
                }
            }
        }
    }
    let (a, p, an) = full_bytes(body, ctx)?;
    if let (Some(k), Some((an, h))) = (&ckey, an) {
        if let Some(st) = state_from(&an, h, 0) {
            cache.put(k, None, st);
        }
    }
    Ok(FixedResult { attribution: a, plan: p, incremental: false, mismatch: None })
}
