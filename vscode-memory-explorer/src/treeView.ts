import * as vscode from 'vscode';
import { MemoryProvider, MemoryFile } from './memoryProvider';
import { MemoryTreeItem } from './treeItem';

export class MemoryTreeView implements vscode.TreeDataProvider<MemoryTreeItem> {
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<
    MemoryTreeItem | undefined | null | void
  >();
  public readonly onDidChangeTreeData: vscode.Event<
    MemoryTreeItem | undefined | null | void
  > = this._onDidChangeTreeData.event;

  constructor(private readonly memoryProvider: MemoryProvider) {}

  public refresh(): void {
    this._onDidChangeTreeData.fire();
  }

  public getTreeItem(element: MemoryTreeItem): vscode.TreeItem {
    return element;
  }

  public async getChildren(element?: MemoryTreeItem): Promise<MemoryTreeItem[]> {
    if (!element) {
      // Root: show tier folders
      return [
        new MemoryTreeItem({ folderTier: 'user' }),
        new MemoryTreeItem({ folderTier: 'project' }),
      ];
    }

    if (element.folderTier) {
      // Inside a folder: show memories for that tier
      const memories = await this.memoryProvider.getMemoriesByTier(element.folderTier);
      return memories.map(m => new MemoryTreeItem({ memory: m }));
    }

    // Inside a memory: no children
    return [];
  }
}