// Mirrors the API's allowlist matching (Python fnmatchcase: * ? [seq]).
import type { Rule } from "./api";

const cache = new Map<string, RegExp>();

export function globToRegExp(p: string): RegExp {
  let re = cache.get(p);
  if (re) return re;
  let out = "";
  for (let i = 0; i < p.length; i++) {
    const c = p[i];
    if (c === "*") out += ".*";
    else if (c === "?") out += ".";
    else if (c === "[") {
      const j = p.indexOf("]", i + 2);
      if (j === -1) out += "\\[";
      else {
        let body = p.slice(i + 1, j).replace(/\\/g, "\\\\");
        if (body.startsWith("!")) body = "^" + body.slice(1);
        out += `[${body}]`;
        i = j;
      }
    } else out += c.replace(/[.+^${}()|\\\]]/g, "\\$&");
  }
  re = new RegExp(`^${out}$`, "s");
  cache.set(p, re);
  return re;
}

export const matches = (value: string, patterns: string[] = []) => patterns.some((p) => globToRegExp(p).test(value));

/** Is an index name allowed by an index-name rule (index-mappings / index-delete)? */
export function indexAllowed(index: string, rule: Rule | undefined): boolean {
  if (!rule) return false;
  const allow = index.startsWith(".") ? rule.allow.filter((p) => p.startsWith(".")) : rule.allow;
  return matches(index, allow) && !matches(index, rule.deny);
}
