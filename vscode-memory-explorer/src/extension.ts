import * as vscode from 'vscode';
import * as path from 'path';
import { MemoryProvider, MemoryFile } from './memoryProvider';
import { MemoryTreeView } from './treeView';

export function activate(context: vscode.ExtensionContext): void {
  console.log('Memory Explorer extension is now active!');

  const memoryProvider = MemoryProvider.getInstance();

  // Create and show Memory Explorer Tree
  const treeDataProvider = new MemoryTreeView(memoryProvider);
  const treeView = vscode.window.createTreeView('memoryTree', {
    treeDataProvider,
    showCollapseAll: true,
  });

  // Initialize Memory Provider and refresh tree
  memoryProvider.init().then(() => {
    console.log(`Loaded ${memoryProvider.memories.length} memories`);
    treeDataProvider.refresh();
  });

  // Register commands
  registerCommands(context, memoryProvider, treeView, treeDataProvider);

  // Set up file watchers for real-time updates
  setupFileWatchers(context, memoryProvider, treeDataProvider);
}

export function deactivate(): void {
  console.log('Memory Explorer extension is now deactivated.');
}

function registerCommands(
  context: vscode.ExtensionContext,
  memoryProvider: MemoryProvider,
  treeView: vscode.TreeView<any>,
  treeDataProvider: MemoryTreeView
): void {
  // View Explorer
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.explorer.view', () => {
      treeView.reveal(undefined, { expand: true });
    })
  );

  // Recall
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.recall', async (query?: string) => {
      const queryStr = query || await vscode.window.showInputBox({
        prompt: 'Recall memories by keyword',
        placeHolder: 'e.g. "design", "security", "frontend"',
      });

      if (queryStr) {
        const results = await memoryProvider.search(queryStr);
        if (results.length > 0) {
          treeView.reveal(results[0].path, { select: true, expand: true });
        } else {
          vscode.window.showInformationMessage('No memories found matching: ' + queryStr);
        }
      }
    })
  );

  // Get by name
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.get', async () => {
      const memories = await memoryProvider.getMemories();
      const selected = await vscode.window.showQuickPick(
        memories.map(m => `${m.metadata.name} (${m.metadata.description.substring(0, 50)}...)`),
        { placeHolder: 'Select a memory' }
      );

      if (selected) {
        const memory = memories.find(m => m.metadata.name === selected.split(' (')[0]);
        if (memory) {
          await vscode.commands.executeCommand('memory.edit', memory);
        }
      }
    })
  );

  // List all
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.list', async () => {
      treeView.reveal(undefined, { expand: true });
    })
  );

  // Validate
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.validate', async () => {
      const memories = await memoryProvider.getMemories();
      const brokenLinks = new Set<string>();

      for (const memory of memories) {
        const links = memory.content.match(/\[\[(.*?)\]\]/g);
        if (links) {
          for (const link of links) {
            const match = link.match(/\[\[(.*?)\]\]/);
            if (match) {
              const linkedName = match[1];
              const target = memories.find(m => m.metadata.name === linkedName);
              if (!target) {
                brokenLinks.add(`${memory.metadata.name} → ${linkedName}`);
              }
            }
          }
        }
      }

      if (brokenLinks.size > 0) {
        const brokenList = Array.from(brokenLinks).join('\n  - ');
        vscode.window.showWarningMessage(`Found ${brokenLinks.size} broken links:\n\n  ${brokenList}`);
      } else {
        vscode.window.showInformationMessage('No broken links found!');
      }
    })
  );

  // Edit (from selection)
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.edit', async (memory?: MemoryFile) => {
      if (!memory) return;

      const doc = await vscode.workspace.openTextDocument(memory.path);
      await vscode.window.showTextDocument(doc);
      await memoryProvider.updateMemoryUsage(memory);
    })
  );

  // Delete (from selection)
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.delete', async (memory?: MemoryFile) => {
      if (!memory) return;

      const confirmed = await vscode.window.showWarningMessage(
        `Delete "${memory.metadata.name}"?`,
        { modal: true, detail: 'This action cannot be undone.' },
        'Delete'
      );

      if (confirmed === 'Delete') {
        await memoryProvider.deleteFile(memory.path);
        vscode.window.showInformationMessage(`Deleted "${memory.metadata.name}"`);
        await memoryProvider.loadMemories();
        treeDataProvider.refresh();
      }
    })
  );

  // Duplicate (from selection)
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.duplicate', async (memory?: MemoryFile) => {
      if (!memory) return;

      const safeName = memory.metadata.name.replace(/[^a-z0-9_-]/gi, '_');
      const newName = await vscode.window.showInputBox({
        prompt: 'Enter new name for duplicate',
        value: `${safeName}_copy`,
      });

      if (newName) {
        const newContent = memory.content.replace(
          /^name:\s*.*$/m,
          `name: ${newName}`
        );
        const newDir = path.dirname(memory.path);
        const newFilePath = path.join(newDir, `${newName}.md`);
        await memoryProvider.writeFile(newFilePath, newContent);
        await memoryProvider.updateMemoryUsage(memory);
        await memoryProvider.loadMemories();
        treeDataProvider.refresh();
        vscode.window.showInformationMessage(`Duplicated to "${newName}"`);
      }
    })
  );

  // Share (from selection)
  context.subscriptions.push(
    vscode.commands.registerCommand('memory.share', async (memory?: MemoryFile) => {
      if (!memory) return;

      const doc = await vscode.workspace.openTextDocument(memory.path);
      const shareUri = await vscode.env.asExternalUri(doc.uri);
      vscode.env.openExternal(shareUri);
    })
  );
}

function setupFileWatchers(
  context: vscode.ExtensionContext,
  memoryProvider: MemoryProvider,
  treeDataProvider: MemoryTreeView
): void {
  const dirs = memoryProvider.getMemoryDirs();

  // If no dirs are scanned yet, wait and retry
  if (dirs.length === 0) {
    // Watch project root for .md creation
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (workspaceRoot) {
      const rootWatcher = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(workspaceRoot, '**/*.md')
      );
      context.subscriptions.push(rootWatcher);
      rootWatcher.onDidChange(() => {
        memoryProvider.loadMemories().then(() => treeDataProvider.refresh());
      });
      rootWatcher.onDidCreate(() => {
        memoryProvider.loadMemories().then(() => treeDataProvider.refresh());
      });
      rootWatcher.onDidDelete(() => {
        memoryProvider.loadMemories().then(() => treeDataProvider.refresh());
      });
    }
    return;
  }

  for (const dir of dirs) {
    const pattern = new vscode.RelativePattern(dir, '*.md');
    const watcher = vscode.workspace.createFileSystemWatcher(pattern);
    context.subscriptions.push(watcher);

    watcher.onDidChange(() => {
      memoryProvider.loadMemories().then(() => treeDataProvider.refresh());
    });

    watcher.onDidCreate(() => {
      memoryProvider.loadMemories().then(() => treeDataProvider.refresh());
    });

    watcher.onDidDelete(() => {
      memoryProvider.loadMemories().then(() => treeDataProvider.refresh());
    });
  }
}