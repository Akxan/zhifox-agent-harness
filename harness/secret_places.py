"""存着密钥的地方：一份名单，两处用。

  commands/paths.py   只读的命令、"只读"那一档用文件工具读工作目录之外的文件：碰到这些地方照常问你
  sandbox/policy.py   命令的沙箱：家目录里的这些地方在任何一档都碰不到（读、写都不行）

为什么放在一起：两边问的是同一件事——"哪些地方存着密钥"。以前各写一份，已经对不上了（只读的判断认 ~/.terraform.d、~/.pgpass、
~/.my.cnf、~/.vault-token，沙箱却不挡），以后加一处也容易只加一边。

两边要的样子不一样：只读的判断能认"名字像"（任何地方叫 id_rsa 的文件），沙箱只认确切的路径。所以名单分成几种：

  NAMES            文件名的模式：路径里任何一段对上就算（*.pem、.env.*、auth.json）。只给只读的判断。
  HOME             家目录下的确切位置，名字也够特别，在别处见到也算（项目里的 .ssh、别人家目录里的 .aws、挂进容器的 .kube）。
                   沙箱挡家目录下的这一处；只读的判断既认家目录下的这一处，也认路径里任何地方连着出现这几段。
  HOME_ONLY        家目录下的确切位置，只认这一处：名字太普通，项目里也常有（.codex、.fly 这种文件夹），
                   或者名字在别处已经由 NAMES 认了（.config/pip/pip.conf 的 pip.conf）。沙箱挡它，只读的判断只在家目录下认它。
  FOLDERS          路径里连着出现这几段就算，没有一个固定的位置（keychains：~/Library/Keychains、/Library/Keychains 都是）。只给只读的判断。
  WINDOWS_HOME     Windows 用户文件夹（%USERPROFILE%）里的确切位置：%APPDATA%（AppData/Roaming）、%LOCALAPPDATA%（AppData/Local）
                   里的那些。和 HOME 一样在别处见到也算；只在按 Windows 的规矩看路径时用，只给只读的判断（Windows 上没有沙箱）。
  WINDOWS_FOLDERS  只在 Windows 上：路径里连着出现这几段就算。

写法：相对于家目录，用 / 分开，大小写照磁盘上的写（沙箱按真实的路径比，Linux 上分大小写）；文件夹末尾带一个 /。
只读的判断比较时不分大小写。

这份名单是猜的，猜不全：只用来多拦一些（多问你一次、沙箱里多挡一处），从不用来放行。拿不准算不算的，宁可放进来；
但太普通的名字（单独的 token、config.json、settings.xml）不能放进"在别处也算"的那几种：哪儿都有，放进去以后几乎每条命令都要问。
这样的写成 HOME_ONLY 里家目录下的确切路径，或者 HOME 里连着的几段（.m2/settings.xml）。
每一项后面写着出处：各个程序的官方文档、手册页，或者它自己的源代码。

Linux 的沙箱还要知道两件事（见 sandbox/linux.py）：
  - 是文件还是文件夹（末尾的 /）：不存在的时候 bwrap 才知道该拿什么占位。
  - GUARDED：不存在也要挡住、不许新建的。别的不存在时不盖：里面本来就没有东西可读，盖上的话 bwrap 会在你真正的家目录里留下一个
    只读的空文件当挂载点（以后 npm login 写不了 .npmrc；空的 settings.xml 让 Maven 报错；~/.docker 的位置上是个文件，
    docker login 就建不了那个文件夹）。只有 ~/.ssh 是新建出来就有用的：里面放一个 authorized_keys，别人就能登进这台电脑。
"""

from __future__ import annotations

import functools
import unicodedata

# 文件名的模式（小写，fnmatch 的写法）：路径里任何一段对上就算
NAMES = (
    # 环境变量文件：各家的 API key 最常放的地方（direnv 的 .envrc 也是）
    ".env", ".env.*", "*.env", ".envrc",
    # 名字里就写着
    "*credential*", "*secret*",
    # 私钥、证书、密钥库、密码库
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx", "*.gpg", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    # 系统的密码文件；进程的环境变量（/proc/*/environ）
    "shadow", "gshadow", "master.passwd", "sudoers", "environ",
    # 命令的历史记录：命令行里常常带着密码、token
    "*_history", ".lesshst",
    # 浏览器的 cookie 和保存的密码
    "cookies", "cookies.sqlite", "login data",
    "wp-config.php",  # WordPress 的数据库密码（developer.wordpress.org/advanced-administration/wordpress/wp-config/）
    ".htpasswd",  # Apache 的密码文件（httpd.apache.org/docs/current/programs/htpasswd.html）
    ".dockercfg",  # 旧版 Docker 的登录信息（新的在 ~/.docker/config.json）
    # Composer 的认证文件：项目里 composer.json 旁边、COMPOSER_HOME 里都有
    # （getcomposer.org/doc/articles/authentication-for-private-packages.md；Composer 自己的代码也是这两处）。Codex 的 ~/.codex/auth.json 也叫这个
    "auth.json",
    # pip 的设置：index-url 里常带着私有源的 token（pip.pypa.io/en/stable/topics/configuration/）。虚拟环境里、/etc 里也有，所以哪儿都算；
    # Windows 上叫 pip.ini（pip 的 configuration.py：CONFIG_BASENAME）
    "pip.conf", "pip.ini",
    "rclone.conf",  # rclone：各个网盘的 token、密码（rclone.org/docs/#config-config-file）；用 Docker 跑时常挂在项目里
    "kaggle.json",  # Kaggle 的 API token（www.kaggle.com/docs/api）
    "settings-security.xml",  # Maven 的主密码，用来解开 settings.xml 里加密过的密码（maven.apache.org/guides/mini/guide-encryption.html）
    "pgpass.conf",  # PostgreSQL 在 Windows 上的密码文件 %APPDATA%\postgresql\pgpass.conf（postgresql.org/docs/current/libpq-pgpass.html）
    "s3cmd.ini",  # s3cmd 在 Windows 上的设置 %APPDATA%\s3cmd.ini（Unix 上是 ~/.s3cfg，见下面；s3cmd 自己的代码里 config_file 的默认值）
    "terraform.rc",  # Terraform 在 Windows 上的设置 %APPDATA%\terraform.rc（developer.hashicorp.com/terraform/cli/config/config-file）
)

# 家目录下的确切位置，在别处见到也算
HOME = (
    # ---- 私钥、密码管理 ----
    ".ssh/",  # OpenSSH：私钥、config、authorized_keys（ssh(1) 手册的 FILES）
    ".gnupg/",  # GnuPG 的私钥（gnupg.org/documentation/manuals/gnupg/GPG-Configuration.html）
    ".password-store/",  # pass（www.passwordstore.org）
    "Library/Keychains/",  # macOS 的钥匙串
    ".local/share/keyrings/",  # GNOME 钥匙圈（wiki.gnome.org/Projects/GnomeKeyring）
    # ---- 云和容器 ----
    ".aws/",  # AWS CLI 的 credentials、config（docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html）
    ".azure/",  # Azure CLI 的登录信息（learn.microsoft.com/cli/azure/azure-cli-configuration）
    ".config/gcloud/",  # Google Cloud CLI（cloud.google.com/sdk/docs/configurations）
    ".kube/",  # kubectl 的 kubeconfig（kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/）
    ".docker/",  # Docker 的 config.json：各个镜像仓库的登录信息（docs.docker.com/reference/cli/docker/#configuration-files）
    ".oci/",  # Oracle Cloud 的 config 和 API 私钥（docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm）
    ".config/doctl/",  # DigitalOcean 的 doctl：access token，Linux 上（docs.digitalocean.com/reference/doctl/reference/auth/init/）
    "Library/Application Support/doctl/",  # 同上，macOS 上
    ".boto",  # gsutil、boto 的设置：HMAC 密钥、OAuth 的 refresh token（cloud.google.com/storage/docs/boto-gsutil）
    ".s3cfg",  # s3cmd 的 access_key、secret_key（s3tools.org/kb/item14.htm）
    ".cloudflared/",  # Cloudflare Tunnel：cert.pem 和隧道的凭据（developers.cloudflare.com/cloudflare-one/connections/connect-networks/）
    ".wrangler/config/",  # Cloudflare 的 wrangler login 存的 OAuth token：旧的位置（workers-sdk 的 global-wrangler-config-path.ts）
    ".config/.wrangler/config/",  # 同上，Linux 上新的位置（XDG）
    "Library/Preferences/.wrangler/config/",  # 同上，macOS 上新的位置
    ".config/ngrok/",  # ngrok 的 authtoken：Linux 上（ngrok.com/docs/agent/config/）
    "Library/Application Support/ngrok/",  # 同上，macOS 上
    ".ngrok2/",  # 同上，ngrok 2 的旧位置
    ".terraform.d/",  # Terraform 的 credentials.tfrc.json（developer.hashicorp.com/terraform/cli/config/config-file#credentials-1）
    ".terraformrc",  # Terraform CLI 的设置：credentials 块（同上）
    ".vault-token",  # HashiCorp Vault 登录以后的 token（developer.hashicorp.com/vault/docs/commands/token-helper）
    # ---- git 和代码托管 ----
    ".gitconfig",  # git：url.<地址>.insteadOf 里带 token 的地址、http.extraHeader 里的认证头（git-config(1) 手册的 FILES）
    ".config/git/",  # 同上，XDG 的那个 config；还有 git-credential-store 的 credentials（git-credential-store(1) 手册的 FILES）
    ".git-credentials",  # git-credential-store 存的明文密码（同上）
    ".config/gh/",  # GitHub CLI 的 hosts.yml（cli.github.com/manual/gh_help_environment 的 GH_CONFIG_DIR）
    ".config/hub",  # hub 的设置文件，里面是 OAuth token（hub.github.com/hub.1.html 的 HUB_CONFIG）
    ".config/github-copilot/",  # GitHub Copilot 编辑器插件存的登录 token（hosts.json、apps.json；github.com/github/copilot.vim）
    ".netrc",  # curl、git、ftp 读的登录名和密码（everything.curl.dev/usingcurl/netrc）
    "_netrc",  # 同上，Windows 上的 curl 找的是这个名字
    # ---- 包管理和发布 ----
    ".npmrc",  # npm 的 _authToken（docs.npmjs.com/cli/configuring-npm/npmrc）
    ".yarnrc.yml",  # Yarn 2 起的 npmAuthToken（yarnpkg.com/configuration/yarnrc）；项目里的那一份也会写
    ".yarnrc",  # Yarn 1 的设置（classic.yarnpkg.com/en/docs/yarnrc），和 .npmrc 一起读，registry 的认证也有人写在这里
    ".pypirc",  # twine 上传 PyPI 用的密码、token（packaging.python.org/en/latest/specifications/pypirc/）
    ".m2/settings.xml",  # Maven：<server> 里的用户名和密码（maven.apache.org/settings.html）
    # Gradle 用户目录里的属性：发布仓库的密码、签名的密码就放在这里，不进项目（docs.gradle.org/current/userguide/build_environment.html）
    ".gradle/gradle.properties",
    ".kaggle/",  # Kaggle 的 kaggle.json（www.kaggle.com/docs/api）
    # Hugging Face 的 token（huggingface.co/docs/huggingface_hub/package_reference/environment_variables 的 HF_TOKEN_PATH、
    # HF_STORED_TOKENS_PATH，默认都在 HF_HOME，也就是 ~/.cache/huggingface 下面）
    ".cache/huggingface/token",
    ".cache/huggingface/stored_tokens",
    ".huggingface/token",  # 同上，旧版 huggingface-cli login 写的位置（huggingface_hub 的 constants.py 里还认它）
    # ---- 数据库 ----
    ".pgpass",  # PostgreSQL 的密码文件（postgresql.org/docs/current/libpq-pgpass.html）
    ".my.cnf",  # MySQL 的选项文件：[client] 里的 password（dev.mysql.com/doc/refman/8.4/en/option-files.html）
    # ---- 别的 ----
    ".config/op/",  # 1Password CLI 的设置和登录信息（developer.1password.com/docs/cli/config-directories）
    ".config/rclone/",  # rclone 的 rclone.conf（rclone.org/docs/#config-config-file）
    ".rclone.conf",  # 同上，旧的位置（还在的话 rclone 先用它）
)

# 家目录下的确切位置，只认这一处
HOME_ONLY = (
    ".codex/",  # OpenAI Codex CLI 的 auth.json、config.toml（CODEX_HOME，默认 ~/.codex；codex 自己的说明）。项目里也会有 .codex 文件夹
    ".composer/",  # Composer 的 auth.json、config.json（COMPOSER_HOME：~/.composer，或者 XDG 的 ~/.config/composer；Composer 自己的代码）
    ".config/composer/",
    ".op/",  # 1Password CLI 的旧位置（developer.1password.com/docs/cli/config-directories）
    ".fly/",  # fly.io 的 flyctl：config.yml 里的 access_token（fly.io/docs/flyctl/）
    ".mc/",  # MinIO Client 的 config.json：各个服务器的 access key、secret key（min.io/docs/minio/linux/reference/minio-mc.html）
    # pip 的几个位置（pip 的 configuration.py 和 utils/appdirs.py：旧的 ~/.pip，XDG 的 ~/.config/pip，macOS 上 Application Support 那个
    # 文件夹在的话用它）。名字由 NAMES 的 pip.conf 在哪儿都认
    ".pip/pip.conf",
    ".config/pip/pip.conf",
    "Library/Application Support/pip/pip.conf",
    ".m2/settings-security.xml",  # Maven 的主密码；名字由 NAMES 认
    ".cargo/credentials.toml",  # crates.io 的 token（doc.rust-lang.org/cargo/reference/config.html#credentials）；名字由 *credential* 认
    ".cargo/credentials",  # 同上，旧的名字
    ".gem/credentials",  # RubyGems 的 API key（guides.rubygems.org/command-reference/#gem-push）
)

# 路径里连着出现这几段就算，没有固定的位置
FOLDERS = (
    "keychains",  # macOS 的钥匙串：~/Library/Keychains，还有整台电脑的 /Library/Keychains
    "keyrings",  # GNOME 钥匙圈和别的钥匙圈
)

# Windows 用户文件夹里的确切位置，在别处见到也算（只在按 Windows 的规矩看路径时用）
WINDOWS_HOME = (
    "AppData/Roaming/gcloud/",  # Google Cloud CLI 在 Windows 上的位置（cloud.google.com/sdk/docs/configurations）
    "AppData/Roaming/gnupg/",  # Gpg4win 的私钥（%APPDATA%\gnupg）
    "AppData/Roaming/GitHub CLI/",  # GitHub CLI 在 Windows 上的位置（cli.github.com/manual/gh_help_environment）
    "AppData/Roaming/terraform.d/",  # Terraform 在 Windows 上的 credentials.tfrc.json（见上面 .terraform.d）
    "AppData/Roaming/doctl/",  # doctl 在 Windows 上（见上面 .config/doctl）
    "AppData/Roaming/rclone/",  # rclone 在 Windows 上（rclone.org/docs/#config-config-file）
    "AppData/Local/github-copilot/",  # GitHub Copilot 插件在 Windows 上（见上面 .config/github-copilot）
    "AppData/Local/ngrok/",  # ngrok 在 Windows 上（ngrok.com/docs/agent/config/）
)

# 只在 Windows 上：路径里连着出现这几段就算
WINDOWS_FOLDERS = (
    "Microsoft/Credentials",  # Windows 凭据管理器（AppData/Roaming 和 AppData/Local 下面都有）
    "Microsoft/Protect",  # DPAPI 的主密钥
    "Microsoft/Vault",  # Windows Vault
    "PSReadLine",  # PowerShell 的命令历史（ConsoleHost_history.txt）
)

# 不存在也要挡住、不许新建的（Linux 的沙箱用，见开头）
GUARDED = (
    ".ssh/",  # 新建一个 authorized_keys，别人就能登进这台电脑；rc 在每次登录时运行（ssh(1)、sshd(8) 手册的 FILES）
)


def _bare(place: str) -> str:
    """去掉文件夹末尾的 /。"""
    return place.rstrip("/")


def home(windows: bool = False) -> tuple[str, ...]:
    """家目录下存着密钥的每一处（用 / 分开，文件夹末尾没有 /）：沙箱挡住它们，只读的判断把它们放在家目录下认。
    windows：再加上 Windows 用户文件夹里的那几处（只读的判断按 Windows 的规矩看时用；沙箱不用，Windows 上没有沙箱）。"""
    return tuple(_bare(place) for place in (*HOME, *HOME_ONLY, *(WINDOWS_HOME if windows else ())))


def folders() -> tuple[str, ...]:
    """home() 里哪几处是文件夹（Linux 的沙箱用：不存在时拿空的文件夹占位，不拿文件）。"""
    return tuple(_bare(place) for place in (*HOME, *HOME_ONLY) if place.endswith("/"))


def guarded() -> tuple[str, ...]:
    """home() 里不存在也要挡住、不许新建的那几处（见 GUARDED）。"""
    return tuple(_bare(place) for place in GUARDED)


@functools.cache
def runs(windows: bool = False) -> tuple[tuple[str, ...], ...]:
    """路径里连着出现就算的几段（每一段统一成 NFC、不分大小写，给只读的判断直接比）：FOLDERS 和 HOME，Windows 上再加两种。"""
    places = (*FOLDERS, *HOME, *((*WINDOWS_FOLDERS, *WINDOWS_HOME) if windows else ()))
    return tuple(tuple(unicodedata.normalize("NFC", part).casefold() for part in _bare(place).split("/")) for place in places)
