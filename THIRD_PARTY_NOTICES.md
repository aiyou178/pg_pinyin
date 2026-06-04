# Third-Party Notices

This repository's original source code remains licensed under MIT. Some release
artifacts may additionally bundle third-party data or model assets. Those assets
keep their own upstream licenses and notices.

## Bundled in packages

### g2pM model assets

- Source: [kakaobrain/g2pM](https://github.com/kakaobrain/g2pM)
- What is bundled: compact exported model assets derived from the official
  `g2pM` wheel during package build
- License: Apache-2.0
- Included notice file: `third_party/g2pm/LICENSE`

## Referenced but not bundled in `_model` packages

### g2pW model assets

- Source: [GitYCC/g2pW](https://github.com/GitYCC/g2pW)
- What is referenced: optional user-installed `g2pw.onnx`, labels, and tokenizer
  files used by the `hybrid_onnx` build
- License: Apache-2.0
- These assets are not bundled in the published `_model` packages in this repo's
  current release layout.
