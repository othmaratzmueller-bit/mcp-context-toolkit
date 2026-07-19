import * as path from 'path';
import { runTests } from 'vscode-test';

async function main(): Promise<void> {
  try {
    // Find VS Code installation
    const version = await findVsCodeVersion();

    // Download and setup VS Code Test Container
    const launcher = await runTests({
      version,
      extensionDevelopmentPath: path.resolve(__dirname, '..'),
      extensionTestsPath: path.resolve(__dirname, 'suite/index'),
    });

    // Wait for tests to complete
    await launcher;
  } catch (err) {
    console.error('Failed to run tests:', err);
    process.exit(1);
  }
}

async function findVsCodeVersion(): Promise<string> {
  // Try to find VS Code in common locations
  const platform = process.platform;
  let vsCodePath: string;

  if (platform === 'darwin') {
    // macOS
    vsCodePath = '/Applications/Visual Studio Code.app';
  } else if (platform === 'win32') {
    // Windows
    vsCodePath = 'C:\\Program Files\\Microsoft VS Code\\Code.exe';
  } else {
    // Linux
    vsCodePath = '/usr/bin/code';
  }

  try {
    const { execSync } = await import('child_process');
    const version = execSync(`"${vsCodePath}" --version`).toString().trim();
    return version;
  } catch {
    // VS Code not found, use latest stable
    return 'stable';
  }
}

main().catch((err) => {
  console.error('Failed to run tests:', err);
  process.exit(1);
});
