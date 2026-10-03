# H3 Studio

MiniMax H3 multimodal workbench with separate image/video/audio references,
timeline guides, native workflow controls, owned assets, jobs and downloads.

The current deployment package runs the **CPU website only**. Generation is
disabled; it does not rent a GPU, carry provider credentials, or download weights.
Public deployment uses password authentication for `superdan` and `supervan`.

- [Lightsail and GitHub Actions deployment](LIGHTSAIL.md)
- [Multi-GPU / API routing design and remaining work](SCALING.zh-CN.md)
- [Generation controls and measured limits](CONTROLS.zh-CN.md)

## Tests

Python 3.12, Node.js and FFmpeg/ffprobe are required:

```sh
python -m pip install -r requirements.lock.txt
python -m unittest discover -s . -p 'test_*.py' -v
node --check web/app.js
```

Tests use disposable data and fake requests. Do not run live generation scripts
as a CI test. Database, media, cloud state, SSH files, model weights, credentials,
logs and local experiments are deliberately excluded from Git and Docker.

GitHub Actions runs CI on push/PR. CPU deployment is an explicit main-branch
workflow dispatch after destination configuration and account provisioning.
The tested image is passed to deployment unchanged. This repository alone does
not create an AWS account, Lightsail instance, domain, GPU, or API subscription.
