#!/bin/bash
###############################################################################
# RayShark — 下载随包 aarch64 二进制依赖 (v2ray-core + mitmproxy)
#
# 目标目录：fnnas.rayshark/app/server/bin/{v2ray, mitmdump}
#   （fnnas.rayshark/ 是干净打包目录，只放 fpk 输入；见 build_on_nas.sh）
#
# 说明：
#  - v2ray-core 官方发布是静态 Go 二进制，直接可在 arm64 Debian(飞牛) 运行。
#  - mitmproxy 官方 linux-aarch64 tar 内含 mitmdump/mitmproxy/mitmweb 独立可执行
#    (PyInstaller onefile 打包，无需系统 Python)。我们只需要 mitmdump。
#
# 这些二进制与运行架构相关，与「构建机」架构无关：可以在 mac 上先下载好放进包，
# 装到飞牛(arm64)后能直接跑。因此本脚本可在 mac / linux 任意机器执行。
#
# 环境变量可覆盖版本：
#   V2RAY_VER=v5.49.0  MITM_VER=12.2.3
###############################################################################
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="${PKG_DIR:-${HERE}/fnnas.rayshark}"
BIN_DIR="${PKG_DIR}/app/server/bin"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

V2RAY_VER="${V2RAY_VER:-v5.49.0}"
MITM_VER="${MITM_VER:-12.2.3}"

V2RAY_URL="https://github.com/v2fly/v2ray-core/releases/download/${V2RAY_VER}/v2ray-linux-arm64-v8a.zip"
MITM_URL="https://downloads.mitmproxy.org/${MITM_VER}/mitmproxy-${MITM_VER}-linux-aarch64.tar.gz"

mkdir -p "${BIN_DIR}"

dl() {
    # dl <url> <out>
    echo "    下载: $1"
    if command -v curl >/dev/null 2>&1; then
        curl -fSL --retry 3 -o "$2" "$1"
    else
        wget -O "$2" "$1"
    fi
}

###############################################################################
echo "==> [1/2] v2ray-core ${V2RAY_VER} (linux arm64-v8a)"
if [ -x "${BIN_DIR}/v2ray" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "    已存在 ${BIN_DIR}/v2ray，跳过 (FORCE=1 可强制重下)"
else
    dl "${V2RAY_URL}" "${TMP_DIR}/v2ray.zip"
    ( cd "${TMP_DIR}" && unzip -o -q v2ray.zip -d v2ray )
    cp "${TMP_DIR}/v2ray/v2ray" "${BIN_DIR}/v2ray"
    chmod +x "${BIN_DIR}/v2ray"
    # 说明：本项目生成的 config 无 routing/geo 规则（仅 vmess 出站 + freedom 直连），
    # 因此不打包 geoip.dat/geosite.dat（共 ~24MB），保持 fpk 精简。
    # 若将来加入按域名/IP 分流路由，需在此拷入 geo 文件并同步 build_config。
    echo "    -> ${BIN_DIR}/v2ray"
fi

###############################################################################
echo "==> [2/2] mitmproxy ${MITM_VER} (linux aarch64, 取 mitmdump)"
if [ -x "${BIN_DIR}/mitmdump" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "    已存在 ${BIN_DIR}/mitmdump，跳过 (FORCE=1 可强制重下)"
else
    dl "${MITM_URL}" "${TMP_DIR}/mitm.tar.gz"
    tar -xzf "${TMP_DIR}/mitm.tar.gz" -C "${TMP_DIR}"
    # tar 内是 mitmproxy / mitmdump / mitmweb 三个独立可执行
    if [ ! -f "${TMP_DIR}/mitmdump" ]; then
        echo "    !! 解压后未找到 mitmdump，压缩包结构可能变化：" >&2
        ls -la "${TMP_DIR}" >&2
        exit 1
    fi
    cp "${TMP_DIR}/mitmdump" "${BIN_DIR}/mitmdump"
    chmod +x "${BIN_DIR}/mitmdump"
    echo "    -> ${BIN_DIR}/mitmdump"
fi

###############################################################################
echo "==> 完成。bin 目录内容："
ls -lh "${BIN_DIR}"
echo ""
echo "校验架构（应为 aarch64 ELF）："
for b in v2ray mitmdump; do
    if command -v file >/dev/null 2>&1; then
        file "${BIN_DIR}/${b}" 2>/dev/null || true
    else
        printf "%s magic: " "${b}"; xxd -l 20 -g 1 "${BIN_DIR}/${b}" 2>/dev/null | head -n1 || true
    fi
done
