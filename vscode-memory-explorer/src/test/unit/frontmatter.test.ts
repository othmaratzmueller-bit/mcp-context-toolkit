/**
 * Headless unit tests — no Electron, no display, no VS Code instance.
 *
 * The suite under src/test/suite/ needs a real editor host; this one covers the
 * pure parsing logic and runs anywhere (`npm run test:unit`), which is what
 * makes it usable in CI and as a pre-commit check.
 */
import * as assert from 'assert';
import { extractMetadata, parseTagList } from '../../frontmatter';

describe('extractMetadata', () => {
  it('reads flat top-level keys', () => {
    const meta = extractMetadata(
      '---\nname: my_memory\ndescription: My test memory\ntype: project\n---\n\nBody.\n',
    );
    assert.strictEqual(meta.name, 'my_memory');
    assert.strictEqual(meta.description, 'My test memory');
    assert.strictEqual(meta.type, 'project');
  });

  it('reads keys nested under a metadata: block', () => {
    // Regression: only top-level keys were parsed, so every memory written in
    // the nested convention (what bundled packages emit) showed up as 'misc'.
    const meta = extractMetadata(
      '---\nname: nested_memory\ndescription: d\nmetadata:\n' +
        '  type: feedback\n  tier: core\n---\n\nBody.\n',
    );
    assert.strictEqual(meta.type, 'feedback');
    assert.strictEqual(meta.name, 'nested_memory');
  });

  it('prefers a top-level key over the nested one', () => {
    const meta = extractMetadata(
      '---\nname: x\ntype: project\nmetadata:\n  type: user\n---\n\nBody.\n',
    );
    assert.strictEqual(meta.type, 'project');
  });

  it('stops reading nested keys at the next top-level key', () => {
    const meta = extractMetadata(
      '---\nname: x\nmetadata:\n  tier: core\ndescription: top level again\n---\n\nB.\n',
    );
    assert.strictEqual(meta.description, 'top level again');
    assert.strictEqual(meta.type, 'misc'); // no type anywhere
  });

  it('degrades an unknown type to misc', () => {
    const meta = extractMetadata('---\nname: x\ntype: not_a_real_type\n---\n\nB.\n');
    assert.strictEqual(meta.type, 'misc');
  });

  it('handles a document without frontmatter', () => {
    const meta = extractMetadata('Just a body, no frontmatter.\n');
    assert.strictEqual(meta.name, 'unnamed');
    assert.strictEqual(meta.description, 'No frontmatter found');
    assert.strictEqual(meta.type, 'misc');
  });

  it('parses tags in YAML flow and JSON syntax, flat or nested', () => {
    assert.deepStrictEqual(
      extractMetadata('---\nname: x\ntags: [a, b, c]\n---\n\nB.\n').tags,
      ['a', 'b', 'c'],
    );
    assert.deepStrictEqual(
      extractMetadata('---\nname: x\ntags: ["a", "b"]\n---\n\nB.\n').tags,
      ['a', 'b'],
    );
    assert.deepStrictEqual(
      extractMetadata('---\nname: x\nmetadata:\n  tags: [n1, n2]\n---\n\nB.\n').tags,
      ['n1', 'n2'],
    );
  });

  it('leaves tags undefined when absent', () => {
    assert.strictEqual(extractMetadata('---\nname: x\n---\n\nB.\n').tags, undefined);
  });

  it('unquotes scalar values like a YAML parser would', () => {
    // Python reads these through real YAML; a raw-string reader would compare
    // '"feedback"' against the known types, miss, and silently fall to 'misc'
    // — and leak the quotes into the description shown in the UI.
    const meta = extractMetadata(
      '---\nname: x\ntype: "feedback"\ndescription: "D"\n---\n\nB.\n',
    );
    assert.strictEqual(meta.type, 'feedback');
    assert.strictEqual(meta.description, 'D');
    assert.strictEqual(
      extractMetadata("---\nname: x\ntype: 'project'\n---\n\nB.\n").type,
      'project',
    );
  });

  it('reads an inline flow mapping for metadata', () => {
    const meta = extractMetadata(
      '---\nname: x\nmetadata: {type: reference, tier: core}\n---\n\nB.\n',
    );
    assert.strictEqual(meta.type, 'reference');
  });

  it('ignores an unterminated frontmatter block', () => {
    const meta = extractMetadata('---\nname: x\ntype: project\n\nno closing fence\n');
    assert.strictEqual(meta.name, 'unnamed');
    assert.strictEqual(meta.type, 'misc');
  });

  it('keeps a colon inside a value intact', () => {
    const meta = extractMetadata(
      '---\nname: x\ndescription: "ratio: 1:2"\n---\n\nB.\n',
    );
    assert.strictEqual(meta.description, 'ratio: 1:2');
  });

  it('tolerates CRLF line endings', () => {
    const meta = extractMetadata('---\r\nname: crlf\r\nmetadata:\r\n  type: user\r\n---\r\n\r\nB.\r\n');
    assert.strictEqual(meta.name, 'crlf');
    assert.strictEqual(meta.type, 'user');
  });
});

describe('parseTagList', () => {
  it('handles empty and bracket-only input', () => {
    assert.deepStrictEqual(parseTagList(''), []);
    assert.deepStrictEqual(parseTagList('[]'), []);
  });

  it('strips quotes and surrounding whitespace', () => {
    assert.deepStrictEqual(parseTagList('[ "a" , b ]'), ['a', 'b']);
  });
});
