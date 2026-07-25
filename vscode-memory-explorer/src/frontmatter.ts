/**
 * Frontmatter parsing for memory files — deliberately free of any `vscode`
 * import so it can be unit-tested headlessly (no Electron, no display).
 *
 * Mirrors the Python engine's `_meta_get` contract: a key is read top-level
 * FIRST and falls back to a nested `metadata:` block. The store carries both
 * styles side by side — flat keys and the `metadata:`-nested convention that
 * bundled packages write — so a reader that only sees top-level keys silently
 * mistypes every nested file as `misc`.
 *
 * Parsing is deliberately string-based (no regex backtracking on untrusted
 * input, no YAML dependency in the extension bundle).
 */

export type MemoryType = 'user' | 'feedback' | 'project' | 'reference' | 'misc';

export interface MemoryMetadata {
  name: string;
  description: string;
  type: MemoryType;
  tags?: string[];
  created?: string;
}

const KNOWN_TYPES = new Set<string>([
  'user',
  'feedback',
  'project',
  'reference',
  'misc',
]);

const FENCE = '---';

/** Strip one layer of matching YAML quotes from a scalar value. */
function unquote(value: string): string {
  const first = value[0];
  const quoted =
    value.length >= 2 &&
    (first === '"' || first === "'") &&
    value.endsWith(first);
  return quoted ? value.slice(1, -1) : value;
}

/** Split `key: value` at the FIRST colon. Returns null if there is none. */
function splitPair(line: string): { key: string; value: string } | null {
  const idx = line.indexOf(':');
  if (idx === -1) {
    return null;
  }
  const key = line.slice(0, idx).trim();
  if (!key) {
    return null;
  }
  return { key, value: unquote(line.slice(idx + 1).trim()) };
}

/** Parse a tag list in JSON (`["a","b"]`) or YAML flow (`[a, b]`) syntax. */
export function parseTagList(raw: string): string[] {
  const trimmed = raw.trim();
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (Array.isArray(parsed)) {
      return parsed.map(String);
    }
  } catch {
    // Not JSON — fall through to YAML flow syntax.
  }
  const inner =
    trimmed.startsWith('[') && trimmed.endsWith(']')
      ? trimmed.slice(1, -1)
      : trimmed;
  return inner
    .split(',')
    .map((t) => unquote(t.trim()))
    .filter((t) => t.length > 0);
}

/**
 * Parse an inline flow mapping — `metadata: {type: feedback, tier: core}`.
 * Returns null when the value is not a flow mapping. Only flat scalar members
 * are supported, which is the whole of what this convention carries.
 */
function parseFlowMapping(value: string): Record<string, string> | null {
  if (!value.startsWith('{') || !value.endsWith('}')) {
    return null;
  }
  const out: Record<string, string> = {};
  for (const part of value.slice(1, -1).split(',')) {
    const pair = splitPair(part);
    if (pair) {
      out[pair.key] = pair.value;
    }
  }
  return out;
}

/** Isolate the frontmatter block, or null when the document has none. */
function frontmatterOf(content: string): string | null {
  const lines = content.split('\n');
  if (lines.length === 0 || lines[0].trimEnd() !== FENCE) {
    return null;
  }
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trimEnd() === FENCE) {
      return lines.slice(1, i).join('\n');
    }
  }
  return null; // unterminated block — treat as "no frontmatter"
}

/**
 * Split a frontmatter block into top-level props and the props nested under a
 * `metadata:` mapping. Only two levels are supported — that is the full extent
 * of the convention, and it keeps the parser dependency-free. Both the block
 * form (`metadata:` + indented keys) and the inline flow form (`metadata: {…}`)
 * are accepted, matching what a real YAML parser — and thus Python — reads.
 */
function splitProps(frontmatter: string): {
  top: Record<string, string>;
  nested: Record<string, string>;
} {
  const top: Record<string, string> = {};
  const nested: Record<string, string> = {};
  let inMetadata = false;

  /** Handle one top-level line; returns whether a `metadata:` block opened. */
  const takeTopLevel = (line: string): boolean => {
    const pair = splitPair(line);
    if (!pair) {
      return false;
    }
    if (pair.key !== 'metadata') {
      top[pair.key] = pair.value;
      return false;
    }
    const flow = parseFlowMapping(pair.value);
    if (flow) {
      Object.assign(nested, flow);
      return false;
    }
    return pair.value === ''; // block form: indented keys follow
  };

  for (const rawLine of frontmatter.split('\n')) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      continue; // blank lines never close a block
    }
    if (line.startsWith(' ') || line.startsWith('\t')) {
      // Indented: a member of an open `metadata:` block, else ignored.
      const pair = inMetadata ? splitPair(line) : null;
      if (pair) {
        nested[pair.key] = pair.value;
      }
      continue;
    }
    // Any top-level key ends an open `metadata:` block.
    inMetadata = takeTopLevel(line);
  }
  return { top, nested };
}

/** Read a key top-level first, then from the nested `metadata:` block. */
function pick(
  top: Record<string, string>,
  nested: Record<string, string>,
  key: string,
): string | undefined {
  const value = top[key] ?? nested[key];
  return value === undefined || value === '' ? undefined : value;
}

/** Extract memory metadata from a markdown document's frontmatter. */
export function extractMetadata(content: string): MemoryMetadata {
  const frontmatter = frontmatterOf(content);
  if (frontmatter === null) {
    return {
      name: 'unnamed',
      description: 'No frontmatter found',
      type: 'misc',
    };
  }

  const { top, nested } = splitProps(frontmatter);
  const rawTags = pick(top, nested, 'tags');
  const rawType = pick(top, nested, 'type');

  return {
    name: pick(top, nested, 'name') ?? 'unnamed',
    description: pick(top, nested, 'description') ?? 'No description',
    // An unknown value degrades to 'misc' rather than being passed through —
    // the field is a closed set downstream (icons, grouping, filters).
    type: (rawType && KNOWN_TYPES.has(rawType) ? rawType : 'misc') as MemoryType,
    tags: rawTags === undefined ? undefined : parseTagList(rawTags),
    created: pick(top, nested, 'created'),
  };
}
