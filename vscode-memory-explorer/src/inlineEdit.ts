import * as vscode from 'vscode';
import * as os from 'os';
import * as path from 'path';
import { MemoryProvider, MemoryFile } from './memoryProvider';

export class InlineEditProvider {
  private static instance: InlineEditProvider | null = null;
  private readonly memoryProvider: MemoryProvider;

  private constructor() {
    this.memoryProvider = MemoryProvider.getInstance();
  }

  public static getInstance(): InlineEditProvider {
    if (!InlineEditProvider.instance) {
      InlineEditProvider.instance = new InlineEditProvider();
    }
    return InlineEditProvider.instance;
  }

  public async editMemoryInEditor(memory: MemoryFile): Promise<void> {
    const doc = await vscode.workspace.openTextDocument(memory.path);
    await vscode.window.showTextDocument(doc, { preview: false });
    await this.memoryProvider.updateMemoryUsage(memory);
  }

  public async showMemoryPreview(memory: MemoryFile): Promise<void> {
    const doc = await vscode.workspace.openTextDocument(memory.path);
    await vscode.window.showTextDocument(doc, { preview: true });
    await this.memoryProvider.updateMemoryUsage(memory);
  }

  public async createMemoryFromSnippet(
    name: string,
    description: string,
    content: string
  ): Promise<void> {
    const memoryDir = await this.memoryProvider.getMemoryDir();
    if (!memoryDir) {
      vscode.window.showErrorMessage(
        'No memory directory found. Please initialize the extension.'
      );
      return;
    }

    const tier = memoryDir.includes(os.homedir())
      ? 'user'
      : 'project';

    const memoryContent = this.createFrontmatter(name, description, content, tier);
    const filePath = path.join(memoryDir, `${name}.md`);

    try {
      await this.memoryProvider.writeFile(filePath, memoryContent);
      vscode.window.showInformationMessage(
        `Created memory "${name}" in ${tier} tier`
      );
    } catch (error) {
      vscode.window.showErrorMessage(`Failed to create memory: ${error}`);
    }
  }

  private createFrontmatter(
    name: string,
    description: string,
    content: string,
    tier: string
  ): string {
    return `---
name: ${name}
description: ${description}
metadata:
  type: ${tier}
---

${content}`;
  }
}