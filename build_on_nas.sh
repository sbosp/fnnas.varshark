#!/bin/bash
###############################################################################
# RayShark — 在飞牛 NAS(aarch64) 上原生编译并打包 fpk
#
# 为什么要有这个脚本？
#   PyInstaller 产物【不能交叉编译】：mac 上打出来的是 Mach-O，装到飞牛(arm64
#   Linux)跑不了。而飞牛系统默认没有带 gevent/flask 的 python3，源码回落也起不来。
#   => 只能在 NAS 本机上，用 NAS 的 python3 把后端 freeze 成 aarch64 ELF 单文件，
#      连同前端静态资源 + 随包 v2ray/mitmdump 二进制一起打进 fpk。之后运行时零依赖。
#
# 【目录约定】(源码 / 打包分离，仿 frontend/ 模式)
#   项目根/
#     server/                    <- 后端源码(dev)：server.py / requirements.txt /
#                                   rayshark/。构建时冻结/拷贝 -> app/server。
#     frontend/                  <- 前端源码(dev)。build -> app/ui
#     fnnas.rayshark/            <- 干净打包目录，只放 fpk 输入(manifest/config/
#                                   cmd/wizard/icon/app)。fnpack 只扫描这里。
#       app/server/              <- 后端构建产物(只放产物，不含源码)：
#                                   rayshark_server(PyInstaller 冻结二进制) +
#                                   bin/(v2ray,mitmdump) + rayshark/mitm_addon.py
#     build*.sh  .build-venv/  .pyi-build/  <- dev 工具，全部在 fnnas.rayshark/ 之外
#
#   后端源码只维护在 server/；本脚本【直接从 server/ 冻结】，不把源码同步进包。
#   包内 app/server 只含编译产物 rayshark_server + bin/；唯一例外是
#   rayshark/mitm_addon.py —— 它被独立 mitmdump 进程用 -s 磁盘加载(非 import)，
#   无法冻进二进制，故必须以单个 .py 随包。
#
# 用法（在飞牛 NAS 上，进入本项目根目录）：
#   chmod +x build_on_nas.sh
#   ./build_on_nas.sh                 # 全量：装依赖 -> PyInstaller -> fnpack
#   SMOKE=1 ./build_on_nas.sh         # 打完额外冒烟启动一次二进制做自检(推荐首次用)
#   PY=/var/apps/pythonX/target/bin/python3 ./build_on_nas.sh   # 手动指定构建用 python3
#   SKIP_BINARIES=1 ./build_on_nas.sh # bin/ 里已有 v2ray+mitmdump，跳过下载
#   REBUILD_FRONTEND=1 ./build_on_nas.sh  # 强制重建前端
#   REBUILD_FRONTEND=0 ./build_on_nas.sh  # 强制复用包内已有 app/ui 产物(跳过重建)
#   (默认：有 frontend/ 源码且有 npm 时自动按源码重建，确保前端改动一定生效)
#
# 前置条件：
#   - 一个可用的 python3 (>=3.8，能 pip install / venv)。飞牛可在应用中心装 "Python"，
#     其路径通常在 /var/apps/python*/target/bin/python3。脚本会自动探测。
#   - node/npm：有 frontend/ 源码时默认据其重建前端(自动 npm install + npm run build)。
#     无 npm 时回落复用随包 app/ui 产物；两者皆无则报错。
#   - 已安装飞牛打包工具 fnpack（fnOS 开发者环境自带）。
#   - 网络可访问 pypi 与 github(下载 pyinstaller/gevent 及随包二进制)。
###############################################################################
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "${HERE}"

# 后端源码目录(dev)：唯一维护点
SRC_SERVER="${HERE}/server"
# 干净打包目录：fnpack 的唯一输入根。所有 fpk 内容都在此目录下。
PKG_DIR="${HERE}/fnnas.rayshark"
PKG_SERVER="${PKG_DIR}/app/server"       # 后端构建产物输出目录
BIN_DIR="${PKG_SERVER}/bin"
DIST_BIN="${PKG_SERVER}/rayshark_server"
# venv 与 PyInstaller 中间产物一律放在打包目录【之外】，避免被打进 fpk
VENV_DIR="${HERE}/.build-venv"
PYI_WORK="${HERE}/.pyi-build"           # PyInstaller build/dist/spec 的临时工作区

SKIP_BINARIES="${SKIP_BINARIES:-0}"
REBUILD_FRONTEND="${REBUILD_FRONTEND:-}"   # 空=自动(有源码+npm则重建)；1=强制重建；0=强制复用
SMOKE="${SMOKE:-0}"

# pip 源：默认清华镜像(国内 NAS 快且稳)；可用 PIP_INDEX_URL=... 覆盖回官方源
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-pypi.tuna.tsinghua.edu.cn}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m%s\033[0m\n' "$*"; }

die() { red "!! $*" >&2; exit 1; }

###############################################################################
info "==> [0/7] 环境自检"
[ -d "${PKG_DIR}" ]    || die "未找到打包目录 ${PKG_DIR}，项目结构异常。"
[ -f "${PKG_DIR}/manifest" ] || die "未找到 ${PKG_DIR}/manifest。"
[ -d "${SRC_SERVER}" ] || die "未找到后端源码目录 ${SRC_SERVER}。"
[ -f "${SRC_SERVER}/server.py" ] || die "未找到 ${SRC_SERVER}/server.py。"
ARCH="$(uname -m)"
if [ "${ARCH}" != "aarch64" ] && [ "${ARCH}" != "arm64" ]; then
    red "!! 当前架构是 ${ARCH}，不是 aarch64。"
    red "   本脚本必须在飞牛 NAS(arm64) 上运行，PyInstaller 产物不能交叉编译。"
    red "   如果只想在 mac 上出「源码回落包」，请改用 ./build.sh SKIP_BACKEND=1。"
    exit 1
fi
green "    架构 OK: ${ARCH}"

command -v fnpack >/dev/null 2>&1 || die "未找到 fnpack。请确认已安装 fnOS 开发者/打包环境。"
green "    fnpack OK: $(command -v fnpack)"
green "    源码目录: ${SRC_SERVER}"
green "    打包目录: ${PKG_DIR}"

###############################################################################
info "==> [1/7] 选定构建用 python3"
pick_python() {
    # 允许用户用 PY=... 手动指定
    if [ -n "${PY:-}" ]; then
        [ -x "${PY}" ] && { echo "${PY}"; return 0; }
        die "指定的 PY=${PY} 不可执行"
    fi
    local cands=()
    # 飞牛 Python 应用运行时（最靠谱，自带 pip）
    for d in /var/apps/python*/target/bin /var/apps/py*/target/bin \
             /vol*/@appdata/python*/target/bin; do
        [ -x "${d}/python3" ] && cands+=("${d}/python3")
    done
    cands+=(/usr/local/bin/python3 /usr/bin/python3)
    command -v python3 >/dev/null 2>&1 && cands+=("$(command -v python3)")
    for py in "${cands[@]}"; do
        [ -x "${py}" ] || continue
        # 需要 3.8+，且能用 venv + pip
        if "${py}" -c 'import sys,venv,ensurepip; sys.exit(0 if sys.version_info>=(3,8) else 1)' >/dev/null 2>&1; then
            echo "${py}"; return 0
        fi
    done
    return 1
}
PYBIN="$(pick_python)" || die "找不到可用于构建的 python3(需 3.8+ 且含 venv/pip)。请在应用中心安装 Python，或用 PY=/path/to/python3 指定。"
green "    使用 python: ${PYBIN}  ($(${PYBIN} -V 2>&1))"

###############################################################################
info "==> [2/7] 准备隔离构建 venv 并安装依赖(含 PyInstaller)  [打包目录之外]"
if [ ! -d "${VENV_DIR}" ]; then
    "${PYBIN}" -m venv "${VENV_DIR}" || die "创建 venv 失败"
fi
VPY="${VENV_DIR}/bin/python"
PIP_OPTS="-i ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST}"
info "    pip 源: ${PIP_INDEX_URL}"
"${VPY}" -m pip install ${PIP_OPTS} --upgrade pip wheel setuptools >/dev/null 2>&1 || info "    (pip 升级跳过)"
info "    安装后端依赖: flask / gevent / gevent-websocket"
"${VPY}" -m pip install ${PIP_OPTS} -r "${SRC_SERVER}/requirements.txt" \
    || die "pip 安装后端依赖失败(检查网络/pypi 源；gevent 需 aarch64 wheel)"
info "    安装 pyinstaller"
"${VPY}" -m pip install ${PIP_OPTS} "pyinstaller>=6.0" \
    || die "pip 安装 pyinstaller 失败"
green "    依赖就绪"

###############################################################################
info "==> [3/7] 随包二进制 (v2ray + mitmdump, aarch64)"
if [ "${SKIP_BINARIES}" != "1" ]; then
    if [ -x "${BIN_DIR}/v2ray" ] && [ -x "${BIN_DIR}/mitmdump" ]; then
        green "    bin/ 已存在，跳过下载(SKIP_BINARIES=1 可显式跳过；FORCE=1 强制重下)"
    else
        ./fetch_binaries.sh
    fi
else
    green "    跳过下载 (SKIP_BINARIES=1)"
fi
for b in v2ray mitmdump; do
    p="${BIN_DIR}/${b}"
    [ -f "${p}" ] || die "缺少 ${p}，请先运行 ./fetch_binaries.sh"
    magic="$(head -c4 "${p}" | od -An -tx1 | tr -d ' \n')"
    [ "${magic}" = "7f454c46" ] || die "${p} 非 ELF(magic=${magic})，架构错误"
done
green "    随包二进制校验通过(ELF)"

###############################################################################
info "==> [4/7] 前端静态资源 -> app/ui"
UI_DIR="${PKG_DIR}/app/ui"
HAS_UI=0
[ -f "${UI_DIR}/index.html" ] && [ -d "${UI_DIR}/assets" ] && HAS_UI=1
HAS_SRC=0
[ -d "${HERE}/frontend" ] && HAS_SRC=1
HAS_NPM=0
command -v npm >/dev/null 2>&1 && HAS_NPM=1

# 决策：源码是唯一真相。只要有 frontend/ 源码且有 npm，就默认重建，
# 避免"包内残留旧产物→复用旧产物→源码改动不生效"的坑(git 里 app/ui 常带旧产物)。
# 仅当无 npm 时才回落复用已有产物；REBUILD_FRONTEND=0 可显式跳过重建强制复用。
DO_BUILD=0
if [ "${REBUILD_FRONTEND}" = "1" ]; then
    DO_BUILD=1                                   # 显式强制重建
elif [ "${REBUILD_FRONTEND}" = "0" ] && [ "${HAS_UI}" = "1" ]; then
    DO_BUILD=0                                   # 显式禁用+已有产物→复用
elif [ "${HAS_SRC}" = "1" ] && [ "${HAS_NPM}" = "1" ]; then
    DO_BUILD=1                                   # 有源码+有 npm→默认重建(源码为准)
elif [ "${HAS_UI}" = "1" ]; then
    DO_BUILD=0                                   # 无 npm 但有现成产物→回落复用
else
    die "app/ui 无已构建前端且未找到 npm。请安装 Node(应用中心/nvm)，或在有 node 的机器上先构建后拷入 ${UI_DIR}。"
fi

if [ "${DO_BUILD}" = "1" ]; then
    [ "${HAS_NPM}" = "1" ] \
        || die "需要重建前端但未找到 npm。请安装 Node，或设 REBUILD_FRONTEND=0 复用已有产物。"
    [ "${HAS_SRC}" = "1" ] || die "未找到前端源码目录 ${HERE}/frontend。"
    if [ "${REBUILD_FRONTEND}" = "1" ]; then
        info "    REBUILD_FRONTEND=1，强制重建前端"
    else
        info "    检测到前端源码，按源码重建前端(如需复用旧产物用 REBUILD_FRONTEND=0)"
    fi
    ( cd "${HERE}/frontend" \
      && { [ -d node_modules ] || { info "    安装 npm 依赖(node_modules 缺失)"; npm install; }; } \
      && info "    执行 npm run build" \
      && npm run build ) || die "前端构建失败"
    [ -d "${HERE}/frontend/dist" ] || die "前端构建未产出 dist 目录"
    # 只覆盖构建产物(assets/index.html)，保留 ui 内的 config/ images/ 等随包静态资源
    rm -rf "${UI_DIR}/assets"
    cp -r "${HERE}/frontend/dist/." "${UI_DIR}/"
    green "    前端已构建并同步到 ${UI_DIR}(含 vConsole chunk)"
else
    green "    复用打包目录内已构建的 app/ui(含 vConsole chunk)"
fi

###############################################################################
info "==> [5/7] 准备后端产物目录 app/server (只保留 bin/，清掉旧源码/产物)"
mkdir -p "${PKG_SERVER}"
# 包内只放编译产物：删掉可能残留的后端源码与旧二进制(bin/ 保留)
rm -rf "${PKG_SERVER}/rayshark"
rm -f  "${PKG_SERVER}/server.py" "${PKG_SERVER}/requirements.txt" "${DIST_BIN}"
green "    已清理(仅留 bin/)，后端将从 ${SRC_SERVER} 直接冻结"

###############################################################################
info "==> [6/7] PyInstaller 冻结后端为 aarch64 单文件"
rm -f "${DIST_BIN}"
rm -rf "${PYI_WORK}"
mkdir -p "${PYI_WORK}"
# 关键：从 SRC_SERVER 源码冻结；--add-data / 入口用绝对路径，避免被 --specpath
# 相对解析找不到；workpath/distpath/specpath 全在 PKG_DIR 之外，不污染 fpk。
( cd "${SRC_SERVER}" && \
  "${VENV_DIR}/bin/pyinstaller" -F --name rayshark_server \
    --paths "${SRC_SERVER}" \
    --collect-all gevent \
    --collect-all geventwebsocket \
    --collect-submodules rayshark \
    --hidden-import flask \
    --hidden-import gevent.monkey \
    --add-data "${SRC_SERVER}/rayshark/mitm_addon.py:rayshark" \
    --workpath "${PYI_WORK}/build" \
    --distpath "${PYI_WORK}/dist" \
    --specpath "${PYI_WORK}" \
    --noconfirm --clean "${SRC_SERVER}/server.py" ) || die "PyInstaller 构建失败"

cp "${PYI_WORK}/dist/rayshark_server" "${DIST_BIN}"
chmod +x "${DIST_BIN}"
rm -rf "${PYI_WORK}"

magic="$(head -c4 "${DIST_BIN}" | od -An -tx1 | tr -d ' \n')"
[ "${magic}" = "7f454c46" ] || die "产物 ${DIST_BIN} 非 ELF(magic=${magic})"
green "    后端二进制就绪: ${DIST_BIN} ($(du -h "${DIST_BIN}" | cut -f1))"

# mitm_addon.py 必须以 .py 随包：mitmdump 用 -s 从磁盘加载，未冻进二进制
mkdir -p "${PKG_SERVER}/rayshark"
cp "${SRC_SERVER}/rayshark/mitm_addon.py" "${PKG_SERVER}/rayshark/mitm_addon.py"
green "    已随包 mitm_addon.py (mitmdump -s 磁盘加载所需的唯一 .py)"

# ---- 可选：冒烟自检(确认冻结产物真的能起来) ----
if [ "${SMOKE}" = "1" ]; then
    info "    冒烟自检：TCP 模式启动二进制并请求 /api/ping"
    SMKVAR="$(mktemp -d)"
    RAYSHARK_TCP=1 RAYSHARK_TCP_PORT=18899 RAYSHARK_REQUIRE_AUTH=0 \
      RAYSHARK_VAR="${SMKVAR}" RAYSHARK_INGEST_PORT=18090 \
      RAYSHARK_WEBROOT="${PKG_DIR}/app/ui" \
      "${DIST_BIN}" > "${SMKVAR}/out.log" 2>&1 &
    SMKPID=$!
    ok=0
    for i in $(seq 1 25); do
        if curl -fs "http://127.0.0.1:18899/app/fnnas-rayshark/api/ping" >/dev/null 2>&1; then
            ok=1; break
        fi
        kill -0 "${SMKPID}" 2>/dev/null || break
        sleep 0.4
    done
    kill "${SMKPID}" 2>/dev/null || true; wait "${SMKPID}" 2>/dev/null || true
    if [ "${ok}" = "1" ]; then
        green "    冒烟自检 PASS：二进制可正常启动并响应 API"
    else
        red   "    冒烟自检 FAIL：二进制未能响应，日志如下："
        tail -n 40 "${SMKVAR}/out.log" >&2 || true
        rm -rf "${SMKVAR}"
        die "冻结产物无法运行，请检查上面日志(通常是缺 hidden-import)"
    fi
    rm -rf "${SMKVAR}"
fi

###############################################################################
info "==> [7/7] 清理打包目录并 fnpack build"
# 只在打包目录内清理，绝不动 frontend/server/.git 等外部内容
find "${PKG_DIR}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${PKG_DIR}" -name '.DS_Store' -delete 2>/dev/null || true
find "${PKG_DIR}" -name '*.pyc' -delete 2>/dev/null || true
rm -f "${PKG_DIR}/app.sock" "${PKG_DIR}"/*.fpk "${HERE}"/*.fpk 2>/dev/null || true

fnpack build --directory "${PKG_DIR}"
# fnpack 只打包 --directory 指向的干净目录；产物落在当前工作目录(项目根)
FPK="$(ls -t "${HERE}"/*.fpk 2>/dev/null | head -n1)"
[ -n "${FPK}" ] || die "fnpack 未产出 fpk"
green ""
green "======================================================================"
green " 打包完成: ${FPK}  ($(du -h "${FPK}" | cut -f1))"
green " 内含 aarch64 PyInstaller 后端二进制，运行时无需系统 python3。"
green " 安装: appcenter-cli install-fpk ${FPK}   或在飞牛应用中心手动上传安装"
green "======================================================================"

cd "${PKG_DIR}"

appcenter-cli install-local
