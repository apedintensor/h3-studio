# 中文字幕运行依赖

章节烧录字幕使用受审的固定字体配置。HTTP请求、项目文件和字幕正文不能指定字体文件、目录、URL或FFmpeg滤镜。

Linux平台镜像的 `runtime-base` 阶段安装 `fonts-noto-cjk=1:20220127+repack1-1` 和fontconfig；该Debian Bookworm包包含 `Noto Sans CJK SC`，字体集合文件为 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`，SC面索引为2。包和源版本来自[Debian官方包页](https://packages.debian.org/bookworm/fonts-noto-cjk)，实际文件见[官方清单](https://packages.debian.org/bookworm/all/fonts-noto-cjk/filelist)。

字体包保留 `/usr/share/doc/fonts-noto-cjk/copyright` 中的许可证与版权信息，遵循其中OFL1.1条款；没有将Windows系统字体复制到镜像或Git仓库。[随包许可说明](https://metadata.ftp-master.debian.org/changelogs/main/f/fonts-noto-cjk/fonts-noto-cjk_20220127%2Brepack1-1_copyright)

固定配置名 `noto-cjk` 对应以上Linux字体。`windows-yahei` 仅供本机Windows预览，引用已存在的 `C:/Windows/Fonts/msyh.ttc`，字体为Microsoft YaHei；不下载、不复制、不当作Linux字体来源。两种字体的字形和度量可能不同，本机预览不能证明生产像素完全相同。

最终容器仍以UID10001运行、根文件系统只读；`XDG_CACHE_HOME=/tmp/sixnine-font-cache` 只将可重建fontconfig缓存写入现有/tmp tmpfs。没有加入模型权重、中央凭据或用户字体。

## 本次验证

2026-10-04构建本地 `sixnine-platform-runtime:captions-20261004`，随后以非root、`--network none`、只读根目录运行，确认：

- `dpkg-query` 返回字体包版本 `1:20220127+repack1-1`。
- `fc-match` 返回精确 `Noto Sans CJK SC`、上述TTC路径与索引2；指定tmpfs缓存后无不可写缓存警告。
- 字体文件和版权文件存在；FFmpeg为Debian `5.1.9-0+deb12u1`。

以上最初只确认运行依赖。后续9项字幕专用测试在Linux现有镜像中无网络、只读、非root运行通过，覆盖不同中文字形不是缺字方框、两行安全留边、单帧字幕、声音与模拟水印保留，以及缺字体不假登记ready。

2026-10-03 22时UTC时段，本地完整应用镜像` sixnine-platform:captions-precommit-20261004`经`tools/check_platform_container.py`实际验收：两段CPU素材与显式音轨，通过HTTP保存/确认中文→v3任务→worker→MP4/FLAC附件→哈希→重启读取；解码检查字幕6–18、30–42半开帧区间。限制为无网络、非root、只读、1GiB/2CPU。该tag是未提交开发源码检查，不是Git revision或生产发布；最终发布仍以真实commit镜像与收据为准。`runtime-base`仅为构建阶段，不能作为正式应用镜像部署。
