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

数据库、上传文件、模型和中文数据默认保存在 Docker volume，Docker 自动创建存储，不需要 `mkdir` 或单独启动初始化服务。外部库目录是现有照片所在的宿主机目录，必须存在。

首次启动或数据不完整时，Compose 会自动通过 GitHub Proxy 下载中文地理数据；数据已经完整时会自动跳过。将文件复制到群晖并命名为 `compose.yaml` 后，只需：

```bash
sudo docker compose -f compose.yaml up -d
```

在本仓库内运行则使用：

```bash
docker compose -f docker-compose.immich.yml up -d
```

确认四个常驻服务正常，`geodata-init` 完成后显示 `Exited (0)` 属于正常情况：

```bash
docker compose -f docker-compose.immich.yml ps
```

新部署不要设置 `IMMICH_UPLOAD_LOCATION` 和 `IMMICH_DB_DATA_LOCATION`。旧 `.env` 若仍配置不存在的目录，会继续触发挂载错误；仅在确认未写入数据的新部署中删除这两项以使用自动 volume。已有数据的部署应保留这两项并指向原目录，切换为 volume 不会自动迁移旧数据。

备份应包含数据库及上传文件的 volume；`docker compose down` 会保留它们，`down -v` 会删除这些数据，不用于常规停止服务。

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
