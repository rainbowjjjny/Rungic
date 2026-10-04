# Native arm64 CI2 bootstrap/packing tools; deliberately separate from the large
# package build host. Fixed Ubuntu OCI base is shared with arm64-host.Dockerfile.
FROM ubuntu@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78
ARG UBUNTU_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports
ARG http_proxy
ARG https_proxy
RUN sed -i "s|http://ports.ubuntu.com/ubuntu-ports/\?|${UBUNTU_MIRROR}/|g" /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      mmdebstrap ubuntu-keyring python3 ca-certificates gnupg \
      apt-utils rsync e2fsprogs gzip xz-utils tar util-linux \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
