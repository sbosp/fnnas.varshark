#!/bin/bash
###############################################################################
# RayShark 构建脚本
#
# 【目录约定】(源码 / 打包分离，仿 frontend/ 模式)
#   项目根/
#     frontend/           <- 前端源码(dev)。build -> 产物同步到 app/ui
#     server/             <- 后端源码(dev)：server.py / requirements.txt / rayshark/
#                            冻结/拷贝 -> 产物同步到 app/server
#     fnnas.rayshark/     <- 干净打包目录(fpk 唯一输入)：manifest/config/cmd/
#                            wizard/icon/app。fnpack 只扫描这里。
#       app/ui/           <- 前端构建产物(由 frontend 同步而来)
#       app/server/       <- 后端构建产物：
#                            bin/            随包 aarch64 二进制 v2ray/mitmdump(下载缓存)
#                            rayshark_server PyInstaller 单二进制(全量模式)
#                            server.py + rayshark/*.py  同步自 server/ (回落 & mitm_addon)
#     build*.sh  fetch_binaries.sh  .build-venv/  .pyi-build/  <- dev 工具，全在打包目录外
#
# 说明：
#   - 后端源码只维护在 server/；构建时同步进 app/server/，从不手改 app/server 里的源码。
#   - rayshark/mitm_addon.py 必须以 .py 形式随包(mitmdump 用 -s 磁盘加载，非 import)，
#     所以无论二进制还是源码模式，都要把源码同步进包。
#
# ⚠️ PyInstaller 后端二进制不能交叉编译：SKIP_BACKEND=1 时走「源码 + NAS python3」回落，
#    mac 上也能出可用 fpk（前提：NAS 有含 gevent/flask 的 python3，或随后在 NAS 上
#    重跑 build_on_nas.sh 用 PyInstaller 固化二进制）。
#
# 用法：
#   ./build.sh                      # 全量（在 aarch64 NAS 上：含 PyInstaller）
#   SKIP_BACKEND=1 ./build.sh       # 仅前端 + 源码后端（mac 上出包）
#   SKIP_FRONTEND=1 ./build.sh      # 前端已 build 过，跳过前端
#   SKIP_BINARIES=1 ./build.sh      # bin/ 已就绪，跳过下载
###############################################################################
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "${HERE}"

# 源码目录(dev)
SRC_SERVER="${HERE}/server"
# 干净打包目录：fnpack 的唯一输入根
PKG_DIR="${HERE}/fnnas.rayshark"
PKG_SERVER="${PKG_DIR}/app/server"   # 后端构建产物输出目录
BIN_DIR="${PKG_SERVER}/bin"
PYI_WORK="${HERE}/.pyi-build"        # PyInstaller 中间产物放打包目录之外

SKIP_FRONTEND="${SKIP_FRONTEND:-0}"
SKIP_BACKEND="${SKIP_BACKEND:-0}"
SKIP_BINARIES="${SKIP_BINARIES:-0}"

[ -d "${PKG_DIR}" ]    || { echo "!! 未找到打包目录 ${PKG_DIR}" >&2; exit 1; }
[ -d "${SRC_SERVER}" ] || { echo "!! 未找到后端源码目录 ${SRC_SERVER}" >&2; exit 1; }

echo "==> [1/6] 随包二进制 (v2ray + mitmdump, aarch64)"
if [ "${SKIP_BINARIES}" != "1" ]; then
    ./fetch_binaries.sh
else
    echo "    跳过下载 (SKIP_BINARIES=1)"
fi
# 硬校验：bin 必须是 aarch64 ELF，否则装到 NAS 必然启动失败
for b in v2ray mitmdump; do
    p="${BIN_DIR}/${b}"
    [ -f "${p}" ] || { echo "    !! 缺少 ${p}，请先运行 ./fetch_binaries.sh" >&2; exit 1; }
    magic="$(head -c4 "${p}" | od -An -tx1 | tr -d ' \n')"
    [ "${magic}" = "7f454c46" ] || { echo "    !! ${p} 非 ELF (magic=${magic})，架构错误" >&2; exit 1; }
done

echo "==> [2/6] 前端构建 (Vue + Vite) -> app/ui"
if [ "${SKIP_FRONTEND}" != "1" ]; then
    ( cd frontend && npm install && npm run build )
    echo "    清理旧 assets 并拷贝 dist -> app/ui（保留 images/config）"
    rm -rf "${PKG_DIR}/app/ui/assets"
    cp -r frontend/dist/assets "${PKG_DIR}/app/ui/assets"
    cp frontend/dist/index.html "${PKG_DIR}/app/ui/index.html"
else
    echo "    跳过前端"
fi

echo "==> [3/6] 同步后端源码 server/ -> app/server (保留 bin/)"
mkdir -p "${PKG_SERVER}"
cp "${SRC_SERVER}/server.py"        "${PKG_SERVER}/server.py"
cp "${SRC_SERVER}/requirements.txt" "${PKG_SERVER}/requirements.txt"
rm -rf "${PKG_SERVER}/rayshark"
cp -r "${SRC_SERVER}/rayshark"      "${PKG_SERVER}/rayshark"
# 清掉源码同步过程中可能带入的缓存
find "${PKG_SERVER}/rayshark" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${PKG_SERVER}/rayshark" -name '*.pyc' -delete 2>/dev/null || true
echo "    源码已同步(server.py / requirements.txt / rayshark/，含 mitm_addon.py)"

echo "==> [4/6] 后端打包"
if [ "${SKIP_BACKEND}" != "1" ]; then
    ARCH="$(uname -m)"
    if [ "${ARCH}" != "aarch64" ] && [ "${ARCH}" != "arm64" ]; then
        echo "    !! 当前架构 ${ARCH} 非 aarch64，PyInstaller 产物无法在 NAS 运行。" >&2
        echo "       如需在 mac 出包，请用 SKIP_BACKEND=1（走源码回落）。" >&2
        exit 1
    fi
    echo "    PyInstaller 打后端 (aarch64 ELF)  [源码=${SRC_SERVER}，中间产物=${PYI_WORK}]"
    rm -rf "${PYI_WORK}"; mkdir -p "${PYI_WORK}"
    # 关键：--add-data / 入口 server.py 用绝对路径，避免被 --specpath 相对解析找不到
    ( cd "${SRC_SERVER}" && \
      pyinstaller -F --name rayshark_server \
        --paths "${SRC_SERVER}" \
        --hidden-import gevent --hidden-import geventwebsocket \
        --hidden-import flask \
        --collect-submodules rayshark \
        --add-data "${SRC_SERVER}/rayshark/mitm_addon.py:rayshark" \
        --workpath "${PYI_WORK}/build" \
        --distpath "${PYI_WORK}/dist" \
        --specpath "${PYI_WORK}" \
        --optimize 2 --noconfirm "${SRC_SERVER}/server.py" )
    cp "${PYI_WORK}/dist/rayshark_server" "${PKG_SERVER}/rayshark_server"
    chmod +x "${PKG_SERVER}/rayshark_server"
    file "${PKG_SERVER}/rayshark_server" || true
    rm -rf "${PYI_WORK}"
else
    echo "    跳过 PyInstaller，随包源码 + NAS python3 回落（见 cmd/main find_python）"
    rm -f "${PKG_SERVER}/rayshark_server" 2>/dev/null || true
fi

echo "==> [5/6] 清理打包目录临时产物"
find "${PKG_DIR}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${PKG_DIR}" -name '.DS_Store' -delete 2>/dev/null || true
find "${PKG_DIR}" -name '*.pyc' -delete 2>/dev/null || true
rm -f "${PKG_DIR}/app.sock" "${PKG_DIR}"/*.fpk "${HERE}"/*.fpk 2>/dev/null || true

echo "==> [6/6] fnpack build"
# fnpack 只打包 --directory 指向的干净目录；产物落在当前工作目录(项目根)，
# 从而打包目录始终只含输入、不含 fpk 产物。
fnpack build --directory "${PKG_DIR}"
FPK="$(ls -t "${HERE}"/*.fpk 2>/dev/null | head -n1)"
[ -n "${FPK}" ] || { echo "!! fnpack 未产出 fpk" >&2; exit 1; }
ls -lh "${FPK}"
echo "==> 完成。安装：appcenter-cli install-fpk ${FPK}"
