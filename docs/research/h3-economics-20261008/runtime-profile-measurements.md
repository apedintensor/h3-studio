# Native runtime profile measurements, 2026-10-08

These are isolated native WanGP experiments, not production-adapter acceptance, an SLA, or a precision-only controlled comparison. Each row is one observed run. Source revision: `0e58385fbde7ff102d276e4a9e490845de76b4ea`; weights repository `DeepBeepMeep/MiniMax-H3`, revision `adc81ccb71352192214d83d5fafb9487e860be39`. All outputs use 124 native frames at 24 fps. No cache-state randomization was performed; a missing loading callback is not a zero-second transfer claim. Total time includes the runner collection path; loading callback time covers only the interval reported as loading_model. It is not download, environment preparation, or total cold boot time.

Only reviewed metadata and artifact hashes are published. Prompts, fixtures, connection details and host paths are omitted. The source receipts remain private audit evidence.

The 5090 pilot used one 32GB-class GPU and 105 GiB container RAM. The PRO experiment used one host with two independent 96GB-class GPUs and 279 GiB shared container RAM, one job per GPU, staged under a host-RAM admission gate. PRO INT8 and BF16 use different loader paths; no all-components-in-VRAM claim is made. Workstation Edition is a permitted candidate, but the observed PRO hardware was Server Edition.

Excluded: all original BF16 v1 gray/noise outputs (incorrect unsplit plain-BF16 QKV path), and 5090 tasks 05, 07, 10 and 14 whose 48-frame video references were shortened to 39 frames upstream. Included video-reference rows used 56 decoded silent frames at 24 fps (17n+5 alignment). Audio references were independent 32kHz stereo inputs; video was ordinary reference conditioning, not forced-prefix continuation or retained soundtrack.

Observed input roles do not establish arbitrary combinations. There is no 768p/50-step sample. No overall perceptual equivalence is asserted.

## h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1

| Sample | Mode / pixels / steps / roles | Total s | Loading callback s | Started UTC | Receipt SHA256 | MP4 SHA256 |
|---|---|---:|---:|---|---|---|
| 5090-01-fl-480p-20 | fl / 832x480 / 20 / none | 129.041689516 | 20.90941387001658 | 2026-10-08T10:02:30.900808Z | `f66db68d2657ad14c4db415adcbc88bd5b9e5cbf7e0a4b585c96537e8387d577` | `5e54f9ca9648ed5da2c9f1861d987264ad6a077fffd5d502ec3a1d67eccf594a` |
| 5090-02-fl-480p-20 | fl / 832x480 / 20 / first_frame,last_frame | 96.791081845 | not emitted | 2026-10-08T10:04:39.996883Z | `204e5269767b0771ae2f46bb94f4d01ab96bad5951a7c7c15bb525d127d55460` | `939db3040b92a3f7cb9f847b53dbd2c0b423e30f870291b4362e3af3e56bef66` |
| 5090-03-ref-480p-20 | ref / 832x480 / 20 / image | 107.275114638 | 19.74610724503873 | 2026-10-08T10:15:39.456466Z | `fe5ff6d2265652d4c95cc9e89f135250c503c83ec8c9b9d7e6e842101fd39647` | `741afa286ec40e9e1b652c1b70f5996912f1b67497507ce26ecc1a57ace2e589` |
| 5090-04-ref-480p-20 | ref / 832x480 / 20 / audio,image | 91.182005124 | not emitted | 2026-10-08T10:19:31.030834Z | `35b1a6e802e346e253eaed7cc5efdd400794075b69967adc0ccd78e33ffb939b` | `1a983f5a2988d7cfd05f2c7b071cff29e2fbfa838a366e493839cf9c9ca75589` |
| 5090-05-fl-480p-50 | fl / 832x480 / 50 / first_frame,last_frame | 236.659655100 | 25.80181234399788 | 2026-10-08T10:28:24.482959Z | `33f8bb091872b15c34c615ea2cc0b34b4f3014a66e0ba75251414254428c4923` | `514440b6b57b2e031d2ada854fc34646ac1aa66c8dd5e146c65a809df099bfe2` |
| 5090-06-fl-768p-20 | fl / 1344x768 / 20 / first_frame,last_frame | 370.602267016 | 12.173774526047055 | 2026-10-08T10:34:00.512077Z | `5374d9ea4b186e08028628b73b3295d76ca1f013b89668a34c571236f4a9004c` | `f3d33a12b4f4aaf42a3b1992f3534e5010d8174a8deafb4761ed6cb7196596d1` |
| 5090-07-ref-480p-50 | ref / 832x480 / 50 / audio,image | 199.287351424 | not emitted | 2026-10-08T10:49:39.891534Z | `731323292954a3cdb83eacce46d2be693c7151496618686739ca1ac75183448a` | `2531804df7d821f1c8fa557076b7a435e608166ef21f10d429f8a50ace11438b` |
| 5090-08-ref-480p-20 | ref / 832x480 / 20 / audio,image,video | 161.568339132 | 4.675859086972196 | 2026-10-08T10:54:39.142036Z | `9591ae7372513db07bc41dc29979db665300985527b483d64cbec55cb28b9ce5` | `317cd32acbfab55d0244725a21826dd27814eb5a4df9a3a56ccd8787598f920b` |

Pinned task config: `int8,int8_convrot,lower_ram`; memory profile 4; SDPA. ConvRot INT8 uses the grouped QKV path. Model asset manifest SHA256: `133f70fca2f37cdaf49cdbd98b48276983cb897bb6a5de5d9e4db00144c70028`.

## h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1

| Sample | Mode / pixels / steps / roles | Total s | Loading callback s | Started UTC | Receipt SHA256 | MP4 SHA256 |
|---|---|---:|---:|---|---|---|
| int8-01-fl-480p-20 | fl / 832x480 / 20 / first_frame,last_frame | 144.795757332 | 52.94602750148624 | 2026-10-08T12:32:13.241600Z | `9a2817c6c4c2ef8400b5a6380f1424ed07db7a22a8ec7680b592565e095a67be` | `1bb65b1e497bb71399c00916902c06077afb93f5d8db00075229da6da5a65734` |
| int8-02-fl-480p-50 | fl / 832x480 / 50 / first_frame,last_frame | 181.687394445 | not emitted | 2026-10-08T12:34:38.046886Z | `1c0f1d383e40b582bb9483182a2f53e4f2cc2004cfc07c787e8c7e68170ce5e8` | `2740afe4d12d3c22ca5b6da2f6b5720a6636a908d0c9c21d2235269ccb3b4a78` |
| int8-03-fl-768p-20 | fl / 1344x768 / 20 / first_frame,last_frame | 281.459483519 | not emitted | 2026-10-08T12:37:39.744503Z | `0e8222d65e8fb80d7c6417bca6074653f311108719c05d95a5f59203c88efbbf` | `ade2f3498e25e1ada542ce7af15174c82cbc5409ffc206c87cc16125930efa7e` |
| int8-04-ref-480p-20 | ref / 832x480 / 20 / audio,image,video | 151.950132221 | 7.467410530894995 | 2026-10-08T12:49:15.065150Z | `330263581638f76329b8114d74a5de4c51825eccdd74e51d4bfd146253916a77` | `66087245480105cdf6971af8c829254cdf81e9e805dd55234f12fd26261f87fc` |
| int8-05-ref-480p-50 | ref / 832x480 / 50 / audio,image,video | 291.045333314 | not emitted | 2026-10-08T12:51:47.027235Z | `3a5cef0552272f19bd1f64867f46caeff0b73844c55ca4fa080bed556594d9d5` | `e591071cbafe795a8c5db9cffdd542e565aa800e3dd34c237e4403f3d8f46128` |
| int8-06-ref-480p-50 | ref / 832x480 / 50 / audio,image | 175.201019116 | not emitted | 2026-10-08T12:56:38.084089Z | `ed8678ba29e1b04bf72cd55d22518a9ef1b22cec8400730b442886e1003543c6` | `cd42bcb0fcb78b3594cfe7e865d8bce3f215ef52185a2c429638a2215b2d8331` |
| int8-07-ref-768p-20 | ref / 1344x768 / 20 / audio,image,video | 495.228468494 | not emitted | 2026-10-08T12:59:33.297230Z | `9c6dfe66de78e0d0dc7282df4844a79ece1ae71087c95d5f249110de2f8bb648` | `5904b85ed6919b1febf25c30954640aa9ea100b0a7e7fbf1f5aba2e60f39e52d` |

Pinned task config: `bf16,bf16,lower_ram`; memory profile 3; SDPA. ConvRot INT8 uses the grouped QKV path. Model asset manifest SHA256: `e8c2d707221f38702e88e8a5a07455fb842b75b06c600a74be301f9567d0245d`.

## h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-splitqkv-v2

| Sample | Mode / pixels / steps / roles | Total s | Loading callback s | Started UTC | Receipt SHA256 | MP4 SHA256 |
|---|---|---:|---:|---|---|---|
| bf16-01-fl-480p-20 | fl / 832x480 / 20 / first_frame,last_frame | 119.088220475 | 11.39671965315938 | 2026-10-08T12:44:17.344753Z | `9eb131561c566f913923e4e6dedd2025ac491ffd6b1c9fb67ff200f13e0abd45` | `27f0eb61183e5edc91dbc12a9b840c0916299a0f0656c5039983f8637c4b3d6d` |
| bf16-02-fl-480p-50 | fl / 832x480 / 50 / first_frame,last_frame | 239.022565279 | 17.459485839121044 | 2026-10-08T12:49:30.039465Z | `92a9579039bdc1c54f5acaa575f982a101b4e133cc798ce0f35e6299e3e0347b` | `422682474dd4b93f8f4f08f7ce8054ceb3de012073934d268cbc3322a454671d` |
| bf16-03-fl-768p-20 | fl / 1344x768 / 20 / first_frame,last_frame | 316.723790203 | not emitted | 2026-10-08T12:53:29.073149Z | `84ff752dbd820615e7e3b5d76e2a829d0751e123accf0814df53982f93e1225f` | `c8059a7aff2f32b3a428d10d19710fafc2001e4d9244724481458d1e39907fee` |
| bf16-04-ref-480p-20 | ref / 832x480 / 20 / audio,image,video | 193.664239204 | 30.833797027356923 | 2026-10-08T13:03:17.656943Z | `563346010e5eac4f8ca89426e1312d8966fe5d4bbc0ae53732e5f99e3750bb2a` | `09a35a50cbdf318f61ef64680d193fc325daef16a928e11093e459fdba29d772` |
| bf16-05-ref-480p-50 | ref / 832x480 / 50 / audio,image,video | 340.303553044 | not emitted | 2026-10-08T13:06:31.332239Z | `7ad1f21155f60d83067619f6c634d454474abfa46be2b0cd86c61dc3d21e4e2b` | `6a1dd4702fee721c3a5189dad6d95c7e0ef69b6a72ce5a3070722491fec4006b` |
| bf16-06-ref-480p-50 | ref / 832x480 / 50 / audio,image | 208.625735211 | not emitted | 2026-10-08T13:12:11.650659Z | `f7f665724d0c12cfdb79beac2b163a7054934d621eaa90ba7995f0307f79a1e7` | `3aee1e45d216b70625dbd2788edf3bfcb19e0ea7a72466ccfaeb05096b2b26b1` |
| bf16-07-ref-768p-20 | ref / 1344x768 / 20 / audio,image,video | 542.692496824 | not emitted | 2026-10-08T13:15:40.288757Z | `329b52a27c8f0005701af92598afe13701ecaa3c9e1444591a1a32770aae93bc` | `7c89964a879b0fdc25ee36b5f9cb896dd50907cc8e157cfda8c9c09fce84f39e` |

Pinned task config: `bf16,bf16`; memory profile 3; SDPA. BF16 v2 requires interleaved QKV splitting enabled. Model asset manifest SHA256: `0af4cb715dd3a918122900e35b33a7ae59b92711e6a274f3e981faa71ff8756e`.
