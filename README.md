# 拼豆图纸生成工具

一款用 Python/Tkinter 编写的桌面工具，用于把普通图片转换为 MARD 色系拼豆图纸。工具支持图片导入、AI 自动抠图、手动选区精修、MARD 色卡匹配、图纸编辑、色号统计以及完整图纸/分色图纸导出。

## 功能特性

- 图片导入：支持 JPG、JPEG、PNG、BMP、WEBP、TIFF、GIF 等常见格式。
- 拖放打开：Windows 下安装 `windnd` 后可直接把图片拖入窗口。
- 自动抠图：通过 `rembg` 自动识别主体并生成前景遮罩，首次使用会下载模型文件。
- 手动选区：提供矩形选框、套索、画笔涂抹，支持添加/擦除选区和撤销。
- 颜色匹配：内置 221 色 MARD 拼豆色卡，使用 CIEDE2000 色差算法匹配更接近人眼感知的颜色。
- 图纸尺寸：支持 52x52、78x78 等常见拼豆板规格，也支持自定义宽高。
- 颜色限制：可设置最大使用颜色数，便于控制备料复杂度。
- 图纸预览：支持缩放、平移、显示/隐藏网格和色号。
- 图纸编辑：生成后可选择色号并局部替换，方便人工修正。
- 导出结果：可导出完整图纸 PNG，也可一次性导出每个色号的分色图纸。

## 项目结构

```text
PictureConverting/
├── README.md                         # 项目说明
├── requirements.txt                  # Python 依赖
├── .gitignore                        # Git 忽略规则
├── src/                              # 源码目录
│   ├── bead_pattern_tool.py          # 主程序入口和 Tkinter GUI
│   ├── auto_cutout.py                # rembg 自动抠图与 mask 处理
│   ├── color_matcher.py              # RGB/LAB 转换与 CIEDE2000 色差匹配
│   └── mard_palette.py               # MARD 221 色色卡数据
├── assets/
│   └── bead.ico                      # Windows 可执行文件图标
├── packaging/
│   └── 拼豆图纸生成工具.spec          # PyInstaller 打包配置
├── examples/
│   ├── input/                        # 示例输入图片
│   └── output/                       # 示例导出结果
└── docs/
    ├── ROADMAP.md                    # 后续优化计划
    └── raw_notes_legacy_encoding.txt # 原始待办记录，保留作参考
```

`build/`、`dist/` 是 PyInstaller 构建产物，体积较大，默认不建议提交到 GitHub。需要发布可执行文件时，建议把 `dist/拼豆图纸生成工具/拼豆图纸生成工具.zip` 上传到 GitHub Releases。

## 运行环境

- 操作系统：Windows 优先，macOS/Linux 可运行源码但拖放和字体效果可能不同。
- Python：建议 3.11 到 3.13。自动抠图依赖的 `rembg>=2.0.76` 需要 Python 3.11+。
- GUI：使用 Python 标准库 `tkinter`。部分 Python 发行版需要额外安装 Tk 支持。

## 安装依赖

建议使用虚拟环境：

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` 使用 `rembg[cpu]`，会同时安装 ONNX Runtime 的 CPU 后端。若你有明确的 CUDA/ROCm 环境，也可以参考 `rembg` 官方说明改用 GPU 后端。

如果只想跳过自动抠图功能，可以不安装 `rembg`，并在软件第一步取消“自动抠出主体”。但完整功能推荐安装全部依赖。

## 阶段 1 AI 自动抠图模型

阶段 1 的“自动抠出主体 (AI)”通过第三方库 [`rembg`](https://github.com/danielgatis/rembg) 完成，当前默认使用 `u2net` 会话。相关上游引用：

- `rembg`：背景移除 Python 库，仓库为 <https://github.com/danielgatis/rembg>，包元数据标注为 MIT License。
- `u2net.onnx`：`rembg` 默认通用主体分割模型，首次运行时会从 `rembg` 的 GitHub Release 下载：<https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx>。
- U^2-Net 原始项目：<https://github.com/xuebinqin/U-2-Net>。

本仓库不内置、不二次分发 `u2net.onnx` 或其他 AI 模型权重；GitHub 提交时保留 README 中的上游引用即可。公开发布或商业分发前，建议按你的发布用途再次复核第三方依赖和模型权重的许可证要求。

### 手动安装模型

联网环境下无需手动处理：首次启用自动抠图时，`rembg` 会自动下载模型并缓存。网络较慢、无法访问 GitHub 或需要离线部署时，可手动安装：

1. 下载模型文件 `u2net.onnx`：

   ```text
   https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx
   ```

2. 放到 `rembg` 默认缓存目录，文件名必须保持为 `u2net.onnx`：

   ```text
   Windows: %USERPROFILE%\.u2net\u2net.onnx
   示例:    C:\Users\<你的用户名>\.u2net\u2net.onnx

   macOS/Linux: ~/.u2net/u2net.onnx
   ```

3. Windows PowerShell 可用下面的命令创建目录并下载：

   ```powershell
   New-Item -ItemType Directory -Force "$env:USERPROFILE\.u2net"
   Invoke-WebRequest `
     -Uri "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx" `
     -OutFile "$env:USERPROFILE\.u2net\u2net.onnx"
   ```

4. 如需放到自定义目录，设置 `U2NET_HOME`，并把模型放到该目录下：

   ```powershell
   $env:U2NET_HOME = "D:\models\u2net"
   New-Item -ItemType Directory -Force $env:U2NET_HOME
   # 将 u2net.onnx 放到 D:\models\u2net\u2net.onnx
   ```

   需要永久生效可执行 `setx U2NET_HOME "D:\models\u2net"`，然后重新打开终端或软件。

## 启动软件

在项目根目录运行：

```bash
python src/bead_pattern_tool.py
```

启动后按照界面中的阶段操作即可。

## 使用流程

### 1. 加载图片

点击“选择图片”导入图片，或在支持拖放的 Windows 环境中直接把图片拖入窗口。可选择是否启用自动抠图。

### 2. 选区精修

如果启用了自动抠图，软件会先生成主体选区。之后可使用矩形选框、套索工具或画笔继续修正：

- 添加到选区：保留更多图像区域。
- 从选区减去：去掉不需要的背景或杂边。
- 重置选区：恢复到自动抠图后的初始状态。
- 全选：把整张图片作为前景处理。
- 撤销：使用 `Ctrl+Z` 撤销最近一次选区或图纸编辑操作。

### 3. 设置图纸

选择拼豆板尺寸、最大颜色数、单颗拼豆显示大小、是否显示网格和色号。软件会将选区内容等比缩放并居中映射到目标拼豆网格。

### 4. 编辑与导出

生成图纸后可以查看色号统计，在预览中放大、平移、切换显示模式，并对局部颜色进行手动修正。确认后可导出：

- 完整图纸：包含坐标、色号和底部色号总览。
- 全部分色图纸：导出一张完整图纸，以及每个使用色号对应的单色施工图。

## 输出文件说明

导出文件通常包含：

```text
bead_pattern_complete.png  # 完整拼豆图纸
bead_pattern_A1.png        # A1 色号分色图纸
bead_pattern_H7.png        # H7 色号分色图纸
...
```

完整图纸底部会统计每个色号需要的拼豆数量，便于购买和备料。

## 核心实现

- `mard_palette.py`：维护 MARD 色号、HEX 和 RGB 数据。
- `color_matcher.py`：把 RGB 转换到 CIE LAB 色彩空间，并使用 CIEDE2000 计算输入颜色与色卡颜色的感知色差。
- `auto_cutout.py`：调用 `rembg` 的 `u2net` 模型移除背景，并把 alpha 通道转换为前景 mask。
- `bead_pattern_tool.py`：管理 GUI 阶段、选区编辑、图纸生成、预览渲染和 PNG 导出。

## 打包 Windows 可执行文件

安装依赖后，在项目根目录执行：

```bash
pyinstaller --clean --noconfirm packaging/拼豆图纸生成工具.spec
```

构建完成后，结果位于：

```text
dist/拼豆图纸生成工具/
```

其中 `拼豆图纸生成工具.exe` 可直接运行。若要分发给普通用户，建议压缩整个 `dist/拼豆图纸生成工具/` 目录，而不是只复制 exe，因为 PyInstaller 的 one-folder 模式还需要 `_internal/` 运行依赖。

## GitHub 提交建议

推荐提交：

- `src/`
- `assets/`
- `packaging/`
- `examples/`
- `docs/`
- `README.md`
- `requirements.txt`
- `.gitignore`

不推荐提交：

- `build/`
- `dist/`
- `__pycache__/`
- `.venv/`
- rembg 下载的模型缓存，例如 `.u2net/`、`u2net.onnx` 或其他 `.onnx` 权重文件

如果需要提供现成安装包，请使用 GitHub Releases 上传压缩包。

## 已知注意事项

- `rembg` 首次自动抠图需要下载 `u2net.onnx` 模型文件，网络较慢时可能等待较久；可按上文手动放到 `.u2net` 缓存目录。
- 大尺寸图纸、较大 bead size 或开启色号显示时，渲染和导出会更耗时。
- 中文字体依赖系统字体，Windows 下效果最佳。
- 色卡 RGB 数据来自整理后的 MARD 色号表，实际拼豆批次、屏幕显示和打印效果可能存在色差。

## 许可

当前仓库尚未包含开源许可证。公开发布前建议根据你的发布意图添加 `LICENSE` 文件，例如 MIT、Apache-2.0 或其他许可证。第三方依赖和 AI 模型由各自上游许可证约束，本项目通过 README 标注引用，不在仓库中提交模型权重。
