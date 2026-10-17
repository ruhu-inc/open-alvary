// Light syntax colouring for the docs' code blocks: strings, numbers, comments, keywords and JSON
// keys, by language. Tokens only, no parsing; anything unrecognised stays plain text.

export type Token = { kind: "plain" | "string" | "number" | "comment" | "keyword" | "key" | "flag"; text: string };

const KEYWORDS: Record<string, string[]> = {
  python: [
    "import",
    "from",
    "def",
    "return",
    "if",
    "else",
    "elif",
    "for",
    "in",
    "while",
    "not",
    "and",
    "or",
    "True",
    "False",
    "None",
    "raise",
    "try",
    "except",
    "with",
    "as",
    "yield",
    "continue",
    "break",
  ],
  javascript: [
    "import",
    "from",
    "const",
    "let",
    "async",
    "await",
    "function",
    "return",
    "if",
    "else",
    "for",
    "of",
    "new",
    "throw",
    "true",
    "false",
    "null",
    "continue",
    "break",
    "yield",
  ],
  bash: ["export", "curl"],
  curl: ["curl"],
};

const RULES: Record<string, RegExp> = {
  json: /(?<key>"(?:[^"\\]|\\.)*"(?=\s*:))|(?<string>"(?:[^"\\]|\\.)*")|(?<number>-?\b\d+(?:\.\d+)?\b)|(?<keyword>\b(?:true|false|null)\b)/g,
  python:
    /(?<comment>#[^\n]*)|(?<string>f?"""[\s\S]*?"""|f?"(?:[^"\\\n]|\\.)*"|f?'(?:[^'\\\n]|\\.)*')|(?<number>\b\d+(?:\.\d+)?\b)|(?<word>\b[A-Za-z_]\w*\b)/g,
  javascript:
    /(?<comment>\/\/[^\n]*)|(?<string>`(?:[^`\\]|\\.)*`|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(?<number>\b\d+(?:\.\d+)?\b)|(?<word>\b[A-Za-z_$][\w$]*\b)/g,
  bash: /(?<comment>(?:^|(?<=\s))#[^\n]*)|(?<string>"(?:[^"\\]|\\.)*"|'[^']*')|(?<flag>(?<=\s)--?[A-Za-z][\w-]*)|(?<word>\b[A-Za-z_][\w-]*\b)/gm,
  http: /(?<keyword>^(?:GET|POST|HTTP\/[\d.]+)\b)|(?<key>^[A-Za-z-]+(?=:))|(?<number>(?<=HTTP\/[\d.]+ )\d{3})/gm,
};

export function tokens(code: string, lang: string): Token[] {
  const language = lang === "curl" ? "bash" : lang === "shell" || lang === "sh" ? "bash" : lang;
  const rule = RULES[language];
  if (!rule) return [{ kind: "plain", text: code }];
  const keywords = new Set(KEYWORDS[lang] ?? KEYWORDS[language] ?? []);
  const out: Token[] = [];
  let at = 0;
  for (const match of code.matchAll(new RegExp(rule.source, rule.flags))) {
    const groups = match.groups ?? {};
    const kind = (Object.keys(groups).find((name) => groups[name] !== undefined) ?? "plain") as Token["kind"] | "word";
    if (kind === "word" && !keywords.has(match[0])) continue;
    if (match.index! > at) out.push({ kind: "plain", text: code.slice(at, match.index) });
    out.push({ kind: kind === "word" ? "keyword" : kind, text: match[0] });
    at = match.index! + match[0].length;
  }
  if (at < code.length) out.push({ kind: "plain", text: code.slice(at) });
  return out;
}
