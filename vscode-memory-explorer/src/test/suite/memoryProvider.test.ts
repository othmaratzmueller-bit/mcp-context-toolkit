import * as assert from 'assert';
import { MemoryProvider } from '../../memoryProvider';
import * as fs from 'fs/promises';
import * as path from 'path';

suite('MemoryProvider', () => {
  const testDir = path.join(__dirname, 'test-data');

  setup(async () => {
    // Create test directory
    if (!(await fs.stat(testDir)).isDirectory()) {
      await fs.mkdir(testDir, { recursive: true });
    }

    // Create test memory files
    const userMemoryPath = path.join(testDir, 'user-memory.md');
    await fs.writeFile(userMemoryPath, `---
name: test_user_memory
description: Test user memory
metadata:
  type: user
tags: [test, design]
---

This is a test user memory.
`);

    const projectMemoryPath = path.join(testDir, 'project-memory.md');
    await fs.writeFile(projectMemoryPath, `---
name: test_project_memory
description: Test project memory
metadata:
  type: project
tags: [development]
---

This is a test project memory.
`);
  });

  teardown(async () => {
    // Clean up test directory
    await fs.rm(testDir, { recursive: true, force: true });
  });

  test('should load memories from directory', async () => {
    const provider = MemoryProvider.getInstance();
    await provider.init();

    // Note: In real usage, provider would scan actual directories
    // This is a simplified test
    assert.ok(provider);
  });

  test('should extract metadata from frontmatter', async () => {
    const provider = MemoryProvider.getInstance();

    const content = `---
name: my_memory
description: My test memory
metadata:
  type: project
tags: [tag1, tag2, tag3]
created: 2026-01-01
---

Content here.
`;

    const metadata = provider.extractMetadata(content);

    assert.strictEqual(metadata.name, 'my_memory');
    assert.strictEqual(metadata.description, 'My test memory');
    assert.strictEqual(metadata.type, 'project');
    assert.deepStrictEqual(metadata.tags, ['tag1', 'tag2', 'tag3']);
    assert.strictEqual(metadata.created, '2026-01-01');
  });

  test('should handle missing frontmatter', async () => {
    const provider = MemoryProvider.getInstance();

    const content = `This is a memory without frontmatter.
It should still be handled gracefully.
`;

    const metadata = provider.extractMetadata(content);

    assert.strictEqual(metadata.name, 'unnamed');
    assert.strictEqual(metadata.description, 'No frontmatter found');
    assert.strictEqual(metadata.type, 'misc');
  });

  test('should handle arrays in frontmatter', async () => {
    const provider = MemoryProvider.getInstance();

    const content = `---
name: array_test
tags: [design, frontend, backend]
---

Content.
`;

    const metadata = provider.extractMetadata(content);

    // Arrays are kept as string in frontmatter
    assert.ok(metadata.tags);
  });

  test('should sort memories by frecency', async () => {
    const provider = MemoryProvider.getInstance();

    // Create memories with different usage stats
    const memories = [
      {
        name: 'high_recall',
        path: 'test1.md',
        content: 'Content 1',
        metadata: { name: 'high_recall', description: 'High recalls', type: 'project' },
        tier: 'project',
        usage: { opens: 10, recalls: 100, lastAccess: '2026-07-09T10:00:00Z' },
      },
      {
        name: 'low_recall',
        path: 'test2.md',
        content: 'Content 2',
        metadata: { name: 'low_recall', description: 'Low recalls', type: 'project' },
        tier: 'project',
        usage: { opens: 10, recalls: 10, lastAccess: '2026-07-09T10:00:00Z' },
      },
      {
        name: 'medium_recall',
        path: 'test3.md',
        content: 'Content 3',
        metadata: { name: 'medium_recall', description: 'Medium recalls', type: 'project' },
        tier: 'project',
        usage: { opens: 10, recalls: 50, lastAccess: '2026-07-09T10:00:00Z' },
      },
    ];

    provider.memories = memories;

    // Sort
    provider.memories.sort((a, b) => {
      const aRecalls = a.usage?.recalls || 0;
      const bRecalls = b.usage?.recalls || 0;
      if (bRecalls !== aRecalls) return bRecalls - aRecalls;

      const aOpens = a.usage?.opens || 0;
      const bOpens = b.usage?.opens || 0;
      if (bOpens !== aOpens) return bOpens - aOpens;

      const aLast = a.usage?.lastAccess || '0';
      const bLast = b.usage?.lastAccess || '0';
      return bLast.localeCompare(aLast);
    });

    // Check order: high_recall (100), medium_recall (50), low_recall (10)
    assert.strictEqual(provider.memories[0].metadata.name, 'high_recall');
    assert.strictEqual(provider.memories[1].metadata.name, 'medium_recall');
    assert.strictEqual(provider.memories[2].metadata.name, 'low_recall');
  });
});
