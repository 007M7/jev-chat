# Resources（构建时生成，不要手写）

这个目录里的三份 JSON 由构建流程从 `tools/jev/` 拷进来：

```
questions.json   七道判断题与两个档案（单聊 / 群聊）
prompts.json     感知层与记忆蒸馏的 prompt
providers.json   服务与模型注册表
```

**它们是 Python 原型、桌面版与 iOS 三端共用的单一来源**，所以仓库里不放副本——抄一份就一定会漂移。
构建时拷贝这一步在 `.github/workflows/ios-unsigned.yml` 里，本地构建时自己拷一次即可：

```bash
mkdir -p ios/Resources
cp tools/jev/questions.json tools/jev/prompts.json tools/jev/providers.json ios/Resources/
```
