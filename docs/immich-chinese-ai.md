# Immich 中文和 AI：照着做

适用版本：`v3.3.5`。当前部署只有一个文件：`docker-compose.immich.yml`。

## 第 1 步：配置外部图库

在 `.env` 填写 Docker 主机上的目录：

```env
IMMICH_EXTERNAL_LIBRARY_1=/srv/photos
IMMICH_EXTERNAL_LIBRARY_2=/mnt/photo-archive
```

每个变量对应一个只读挂载：

| 环境变量 | Immich 路径 |
| --- | --- |
| `IMMICH_EXTERNAL_LIBRARY_1` | `/external/1` |
| `IMMICH_EXTERNAL_LIBRARY_2` | `/external/2` |
| `IMMICH_EXTERNAL_LIBRARY_3` | `/external/3` |
| `IMMICH_EXTERNAL_LIBRARY_4` | `/external/4` |

需要 SMB/NFS 时，先把共享挂载到 Docker 主机，再填写挂载后的目录。不要在 Compose 中填写另一台机器的路径。

## 第 2 步：启动

首次启动或数据不完整时，Compose 会自动通过 GitHub Proxy 下载中文地理数据；数据已经完整时会自动跳过：

```bash
docker compose -f docker-compose.immich.yml up -d
```

确认四个服务正常：

```bash
docker compose -f docker-compose.immich.yml ps
```

## 第 3 步：设置中文界面

1. 登录 Immich。
2. 点击右上角头像，进入 **Account Settings**。
3. 找到 **Language**，选择 **简体中文**。
4. 刷新页面。

语言按账号保存，每个账号分别设置。

## 第 4 步：设置 AI 搜索

1. 用管理员账号进入 **Administration → Settings → Machine Learning Settings**。
2. 确认 Machine Learning、Smart Search、Facial Recognition 和 OCR 已启用。
3. Smart Search 的模型选择 `ViT-B-16-SigLIP2__webli` 并保存。
4. 进入 **Administration → Jobs**，点击 Smart Search 右侧的 **All**。
5. 如果人物或文字搜索没有结果，再执行 Face Detection、Facial Recognition 和 OCR 的 **All**。

第一次使用模型时会自动下载。模型缓存保存在 Docker volume `immich_model_cache`。当前 Compose 已设置一个 worker 和 300 秒 TTL，以减少内存占用。

## 第 5 步：生成中文地点

`immich-geodata-cn` 已作为只读数据挂载到 Immich，不需要另起 ReverseGeo 服务。更新数据时执行：

```bash
IMMICH_GEODATA_CN_FORCE=1 docker compose -f docker-compose.immich.yml up -d --force-recreate geodata-init
docker compose -f docker-compose.immich.yml up -d --force-recreate immich-server
```

然后在 **Administration → Jobs → Metadata Extraction** 点击 **All**，让已有照片重新生成地点信息。新照片会在元数据提取时使用这套数据。

检查导入日志：

```bash
docker compose -f docker-compose.immich.yml logs immich-server | grep 'geodata records imported'
```

该项目主要覆盖中国大陆、港澳台，并实验性支持部分海外地区；它不是全球中文地名数据库。[项目说明](https://github.com/ZingLix/immich-geodata-cn)
