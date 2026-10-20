#!/usr/bin/env sh
set -eu

# Example only. Create /etc/aura/smb-credentials with mode 600 and these keys:
#   username=photo-reader
#   password=REDACTED
#   domain=WORKGROUP
# Then create a persistent systemd mount or fstab entry. Do not commit the
# credentials file and do not put its contents in .env.

sudo install -d -m 0755 /mnt/aura-photos
sudo mount -t cifs //NAS_HOST/PHOTO_SHARE /mnt/aura-photos \
  -o credentials=/etc/aura/smb-credentials,ro,vers=3.0,iocharset=utf8,uid=$(id -u),gid=$(id -g),_netdev
