import * as vscode from 'vscode';
import { MemoryFile } from './memoryProvider';

type FrecencyLevel = 'hot' | 'warm' | 'cold';

export class MemoryTreeItem extends vscode.TreeItem {
  public readonly memory: MemoryFile | null;
  public readonly folderTier: 'user' | 'project' | null;

  constructor(options: {
    memory?: MemoryFile;
    folderTier?: 'user' | 'project';
  }) {
    const label = options.folderTier
      ? options.folderTier === 'user'
        ? '👤 User Memories'
        : '📁 Project Memories'
      : options.memory!.metadata.name;

    super(label);

    this.memory = options.memory || null;
    this.folderTier = options.folderTier || null;

    if (options.folderTier) {
      this.iconPath = options.folderTier === 'user'
        ? new vscode.ThemeIcon('account')
        : new vscode.ThemeIcon('folder-library');
      this.collapsibleState = vscode.TreeItemCollapsibleState.Expanded;
      this.contextValue = 'memory-folder';
    } else if (options.memory) {
      this.description = this.buildDescription(options.memory);
      this.tooltip = this.buildTooltip(options.memory);
      this.iconPath = this.getMemoryIcon(options.memory);
      this.collapsibleState = vscode.TreeItemCollapsibleState.None;
      this.contextValue = 'memory-item';
      this.resourceUri = vscode.Uri.file(options.memory.path);
      this.command = {
        command: 'memory.edit',
        title: 'Edit',
        arguments: [options.memory],
      };
    }
  }

  private buildDescription(memory: MemoryFile): string {
    const parts: string[] = [memory.metadata.type];
    if (memory.metadata.tags && memory.metadata.tags.length > 0) {
      parts.push(memory.metadata.tags.slice(0, 2).join(', '));
    }
    return parts.join(' • ');
  }

  private buildTooltip(memory: MemoryFile): string {
    const lastAccess = memory.usage?.lastAccess
      ? new Date(memory.usage.lastAccess).toLocaleString()
      : 'Never';

    return [
      `🧠 ${memory.metadata.name}`,
      `---`,
      memory.metadata.description,
      '',
      `Type: ${memory.metadata.type}`,
      `Tier: ${memory.tier}`,
      `Path: ${memory.path}`,
      '',
      `Opens: ${memory.usage?.opens || 0}`,
      `Recalls: ${memory.usage?.recalls || 0}`,
      `Last: ${lastAccess}`,
    ].join('\n');
  }

  private getMemoryIcon(memory: MemoryFile): vscode.ThemeIcon {
    switch (memory.metadata.type) {
      case 'feedback':
        return new vscode.ThemeIcon('feedback');
      case 'project':
        return new vscode.ThemeIcon('project');
      case 'reference':
        return new vscode.ThemeIcon('bookmark');
      case 'user':
        return new vscode.ThemeIcon('person');
      default:
        return new vscode.ThemeIcon('note');
    }
  }

  public getFrecencyLevel(): FrecencyLevel {
    const recalls = this.memory?.usage?.recalls || 0;
    if (recalls >= 10) return 'hot';
    if (recalls >= 3) return 'warm';
    return 'cold';
  }
}