const path = require('path');

module.exports = {
  mode: 'none',
  entry: './src/extension.ts',
  target: 'node',
  output: {
    library: {
      type: 'commonjs2',
    },
    filename: 'extension.js',
    path: path.resolve(__dirname, 'out'),
  },
  externals: {
    vscode: 'commonjs vscode',
  },
  resolve: {
    extensions: ['.ts', '.js'],
  },
  module: {
    rules: [
      {
        test: /\.ts$/,
        exclude: /node_modules/,
        use: [
          {
            loader: 'ts-loader',
          },
        ],
      },
    ],
  },
};
