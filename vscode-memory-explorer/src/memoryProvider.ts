import * as vscode from 'vscode';
import * as fs from 'fs/promises';
import * as path from 'path';
import { extractMetadata, MemoryMetadata } from './frontmatter';

export interface MemoryFile {
  name: string;
  path: string;
  content: string;
  metadata: {
    name: string;
    description: string;
    type: 'user' | 'feedback' | 'project' | 'reference' | 'misc';
    tags?: string[];
    created?: string;
  };
  tier: 'user' | 'project';
  usage?: {
    opens: number;
    recalls: number;
    lastAccess: string;
  };
}

export class MemoryProvider {
  private static instance: MemoryProvider | null = null;
  public memories: MemoryFile[] = [];
  private memoryDirs: string[] = [];
  private readonly homeDir: string =
    process.env.HOME || process.env.USERPROFILE || '';

  // Project-tier memory conventions, tried in order — mirrors the engine's
  // auto-discovery (CONTEXT_STORE_CONVENTIONS, same env var + same default
  // order the engine's core.py::store_conventions() uses). `.context` is the
  // generic default; `.claude` is a fallback for existing Claude Code repos.
  // Overridable so an embedding product (e.g. `.talos`) can brand its store
  // dir without forking this extension.
  private readonly projectMemoryDirs: string[] = (() => {
    const raw = (process.env.CONTEXT_STORE_CONVENTIONS || '').trim();
    const conventions = raw
      ? raw.split(',').map((c) => c.trim()).filter(Boolean)
      : ['.context', '.claude'];
    return conventions.map((c) => path.join(c, 'memory'));
  })();

  // User-tier (cross-project) memory. Overridable via CONTEXT_USER_MEMORY_DIR
  // (the same env var the engine reads); falls back to ~/.context/memory.
  private readonly userMemoryDir: string =
    process.env.CONTEXT_USER_MEMORY_DIR ||
    path.join(this.homeDir, '.context', 'memory');

  private constructor() {}

  public static getInstance(): MemoryProvider {
    if (!MemoryProvider.instance) {
      MemoryProvider.instance = new MemoryProvider();
    }
    return MemoryProvider.instance;
  }

  public async init(): Promise<void> {
    await this.scanMemoryDirs();
  }

  private async scanMemoryDirs(): Promise<void> {
    this.memoryDirs = [];

    // Scan project memory dirs — try each convention under every workspace root.
    const workspaceRoots = (vscode.workspace.workspaceFolders || []).map(
      (f) => f.uri.fsPath
    );
    for (const root of workspaceRoots.length ? workspaceRoots : ['.']) {
      for (const conv of this.projectMemoryDirs) {
        const projectMemoryPath = path.join(root, conv);
        if (await this.dirExists(projectMemoryPath)) {
          this.memoryDirs.push(projectMemoryPath);
        }
      }
    }

    // Scan user memory dir
    if (await this.dirExists(this.userMemoryDir)) {
      this.memoryDirs.push(this.userMemoryDir);
    }

    await this.loadMemories();
  }

  private async dirExists(p: string): Promise<boolean> {
    try {
      const stat = await fs.stat(p);
      return stat.isDirectory();
    } catch {
      return false;
    }
  }

  public async loadMemories(): Promise<void> {
    this.memories = [];

    for (const dir of this.memoryDirs) {
      try {
        const entries = await fs.readdir(dir, { withFileTypes: true });

        for (const entry of entries) {
          if (entry.isFile() && entry.name.endsWith('.md')) {
            const filePath = path.join(dir, entry.name);
            const content = await fs.readFile(filePath, 'utf-8');
            const tier = this.getTierByDir(dir);
            const metadata = this.extractMetadata(content);

            this.memories.push({
              name: metadata.name || entry.name.replace('.md', ''),
              path: filePath,
              content,
              metadata,
              tier,
              usage: undefined,
            });
          }
        }
      } catch {
        // Directory doesn't exist or can't be read — skip
      }
    }

    this.sortByFrecency();
  }

  private getTierByDir(dir: string): 'user' | 'project' {
    if (dir.startsWith(this.homeDir) || dir === this.userMemoryDir) {
      return 'user';
    }
    return 'project';
  }

  /**
   * Extract memory metadata from frontmatter.
   *
   * Delegates to the vscode-free `frontmatter` module so the parsing rules
   * (top-level key first, then the nested `metadata:` block — matching the
   * Python engine's `_meta_get`) are unit-testable without an Electron host.
   */
  public extractMetadata(content: string): MemoryMetadata {
    return extractMetadata(content);
  }

  private sortByFrecency(): void {
    this.memories.sort((a, b) => {
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
  }

  public async getMemories(): Promise<MemoryFile[]> {
    return this.memories;
  }

  public async getMemoryByName(name: string): Promise<MemoryFile | null> {
    return this.memories.find(m => m.metadata.name === name) || null;
  }

  public async getMemoriesByTier(tier: 'user' | 'project'): Promise<MemoryFile[]> {
    return this.memories.filter(m => m.tier === tier);
  }

  public async getMemoriesByType(type: 'user' | 'feedback' | 'project' | 'reference' | 'misc'): Promise<MemoryFile[]> {
    return this.memories.filter(m => m.metadata.type === type);
  }

  public async getHotMemories(limit: number = 10): Promise<MemoryFile[]> {
    return this.memories.slice(0, limit);
  }

  public async search(query: string): Promise<MemoryFile[]> {
    const lowerQuery = query.toLowerCase();
    return this.memories.filter(m => {
      return (
        m.metadata.name.toLowerCase().includes(lowerQuery) ||
        m.metadata.description.toLowerCase().includes(lowerQuery) ||
        m.content.toLowerCase().includes(lowerQuery) ||
        m.metadata.tags?.some(tag => tag.toLowerCase().includes(lowerQuery))
      );
    });
  }

  public async updateMemoryUsage(memory: MemoryFile): Promise<void> {
    const index = this.memories.findIndex(m => m.path === memory.path);
    if (index !== -1) {
      this.memories[index].usage = {
        opens: (this.memories[index].usage?.opens || 0) + 1,
        recalls: (this.memories[index].usage?.recalls || 0) + 1,
        lastAccess: new Date().toISOString(),
      };
      this.sortByFrecency();
    }
  }

  public async readFile(filePath: string): Promise<string> {
    return fs.readFile(filePath, 'utf-8');
  }

  public async writeFile(filePath: string, content: string): Promise<void> {
    await fs.writeFile(filePath, content, 'utf-8');
  }

  public async deleteFile(filePath: string): Promise<void> {
    await fs.unlink(filePath);
  }

  public async getMemoryDir(): Promise<string> {
    return this.memoryDirs[0] || '';
  }

  public getMemoryDirs(): string[] {
    return this.memoryDirs;
  }
}
