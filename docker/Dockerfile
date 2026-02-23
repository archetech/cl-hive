# cl-hive Production Node
# Core Lightning + Tor + WireGuard + Full Plugin Stack
#
# Build: docker build -t cl-hive-node .
# Run:   docker-compose up -d

FROM ubuntu:24.04

LABEL maintainer="Lightning Goats Team"
LABEL version="2.2.8"
LABEL description="Production Lightning node with cl-hive coordination"

# Prevent interactive prompts during install
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# =============================================================================
# SYSTEM PACKAGES
# =============================================================================

RUN apt-get update && apt-get install -y \
    # Build essentials
    build-essential \
    autoconf \
    automake \
    libtool \
    pkg-config \
    git \
    curl \
    wget \
    # Core Lightning dependencies
    libgmp-dev \
    libsqlite3-dev \
    libpq5 \
    python3 \
    python3-pip \
    python3-venv \
    net-tools \
    zlib1g-dev \
    libsodium-dev \
    gettext \
    # Tor
    tor \
    # WireGuard
    wireguard \
    wireguard-tools \
    # Networking utilities
    iproute2 \
    iptables \
    dnsutils \
    # Process management
    supervisor \
    # JSON processing
    jq \
    # File watching for backup
    inotify-tools \
    # Node.js for c-lightning-REST
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# BITCOIN CLI (required for CLN's bcli plugin)
# =============================================================================
# NOTE: bitcoin-cli is mounted from host via docker-compose.yml
# The download step is skipped to avoid network issues with bitcoincore.org
# If you need bitcoin-cli baked into the image, uncomment below:
#
# ARG BITCOIN_VERSION=28.1
# RUN ARCH=$(uname -m) \
#     && if [ "$ARCH" = "x86_64" ]; then ARCH="x86_64-linux-gnu"; fi \
#     && if [ "$ARCH" = "aarch64" ]; then ARCH="aarch64-linux-gnu"; fi \
#     && curl -SLO "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/bitcoin-${BITCOIN_VERSION}-${ARCH}.tar.gz" \
#     && tar -xzf bitcoin-${BITCOIN_VERSION}-${ARCH}.tar.gz \
#     && install -m 0755 bitcoin-${BITCOIN_VERSION}/bin/bitcoin-cli /usr/local/bin/ \
#     && rm -rf bitcoin-${BITCOIN_VERSION} bitcoin-${BITCOIN_VERSION}-${ARCH}.tar.gz

# =============================================================================
# CORE LIGHTNING
# =============================================================================
# AMD64: Use pre-built binary from GitHub releases (fast)
# ARM64: Build from source (CLN doesn't publish ARM64 Ubuntu 24.04 binaries)

ARG CLN_VERSION=v25.12.1

# Install build dependencies for ARM64 source compilation
RUN apt-get update && apt-get install -y \
    libpq-dev \
    valgrind \
    cppcheck \
    shellcheck \
    libsecp256k1-dev \
    lowdown \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# PYTHON ENVIRONMENT
# =============================================================================

# Create virtual environment early so CLN ARM64 source build can use pip
RUN python3 -m venv /opt/cln-plugins-venv
ENV PATH="/opt/cln-plugins-venv/bin:$PATH"

# Install CLN: pre-built on AMD64, source build on ARM64
RUN ARCH=$(uname -m) \
    && if [ "$ARCH" = "x86_64" ]; then \
        echo "Installing pre-built CLN for AMD64..." \
        && curl -SLO "https://github.com/ElementsProject/lightning/releases/download/${CLN_VERSION}/clightning-${CLN_VERSION}-Ubuntu-24.04-amd64.tar.xz" \
        && tar -xf clightning-${CLN_VERSION}-Ubuntu-24.04-amd64.tar.xz -C /usr/local --strip-components=2 \
        && rm clightning-${CLN_VERSION}-Ubuntu-24.04-amd64.tar.xz; \
    elif [ "$ARCH" = "aarch64" ]; then \
        echo "Building CLN from source for ARM64..." \
        && pip install --no-cache-dir mako grpcio-tools \
        && git clone --depth 1 --branch ${CLN_VERSION} https://github.com/ElementsProject/lightning.git /tmp/lightning \
        && cd /tmp/lightning \
        && pip install --no-cache-dir -r requirements.txt \
        && ./configure --prefix=/usr/local \
        && make -j$(nproc) \
        && make install \
        && cd / \
        && rm -rf /tmp/lightning; \
    fi \
    && ldconfig

# Install Python dependencies
RUN pip install --no-cache-dir \
    pyln-client>=24.0 \
    PyNaCl>=1.5.0 \
    requests \
    anthropic

# =============================================================================
# CLBOSS (REQUIRED - ksedgwic fork with clboss-unmanage support)
# =============================================================================
# CLBOSS provides automated channel management and liquidity optimization.
# Required for cl-hive fleet coordination.

WORKDIR /tmp

RUN apt-get update && apt-get install -y \
    libev-dev \
    libcurl4-openssl-dev \
    autoconf-archive \
    libunwind-dev \
    && rm -rf /var/lib/apt/lists/*

# Build CLBOSS from ksedgwic fork (has clboss-unmanage support)
RUN git clone --depth 1 https://github.com/ksedgwic/clboss.git \
    && cd clboss \
    && mkdir -p m4 \
    && autoreconf -fi \
    && ./configure \
    && make -j$(nproc) \
    && make install \
    && cd .. \
    && rm -rf clboss

# =============================================================================
# VITALITY PLUGIN (REQUIRED)
# =============================================================================
# Vitality monitors channel health, gossip health, and pings Amboss for online status.
# Essential for production deployments to maintain uptime and Amboss visibility.
# Config: vitality-amboss=true (set in docker-entrypoint.sh)

ARG VITALITY_VERSION=v0.2.3
RUN ARCH=$(uname -m) \
    && if [ "$ARCH" = "x86_64" ]; then TRIPLE="x86_64-linux-gnu"; fi \
    && if [ "$ARCH" = "aarch64" ]; then TRIPLE="aarch64-linux-gnu"; fi \
    && wget -O /tmp/vitality.tar.gz "https://github.com/daywalker90/vitality/releases/download/${VITALITY_VERSION}/vitality-${VITALITY_VERSION}-${TRIPLE}.tar.gz" \
    && tar -xzf /tmp/vitality.tar.gz -C /tmp \
    && mv /tmp/vitality /usr/local/bin/vitality \
    && chmod +x /usr/local/bin/vitality \
    && rm /tmp/vitality.tar.gz

# =============================================================================
# SLING PLUGIN (REQUIRED)
# =============================================================================
# Sling provides rebalancing capabilities required by cl-revenue-ops.

ARG SLING_VERSION=v4.1.3
RUN ARCH=$(uname -m) \
    && if [ "$ARCH" = "x86_64" ]; then TRIPLE="x86_64-linux-gnu"; fi \
    && if [ "$ARCH" = "aarch64" ]; then TRIPLE="aarch64-linux-gnu"; fi \
    && wget -O /tmp/sling.tar.gz "https://github.com/daywalker90/sling/releases/download/${SLING_VERSION}/sling-${SLING_VERSION}-${TRIPLE}.tar.gz" \
    && tar -xzf /tmp/sling.tar.gz -C /tmp \
    && mv /tmp/sling /usr/local/bin/sling \
    && chmod +x /usr/local/bin/sling \
    && rm /tmp/sling.tar.gz

# =============================================================================
# BOLTZ CLIENT (Submarine/Reverse Swaps)
# =============================================================================
ARG BOLTZ_VERSION=v2.11.0
RUN ARCH=$(uname -m) \
    && if [ "$ARCH" = "x86_64" ]; then DL_SUFFIX="linux-amd64"; TAR_DIR="linux_amd64"; fi \
    && if [ "$ARCH" = "aarch64" ]; then DL_SUFFIX="linux-arm64"; TAR_DIR="linux_arm64"; fi \
    && wget -O /tmp/boltz-client.tar.gz \
       "https://github.com/BoltzExchange/boltz-client/releases/download/${BOLTZ_VERSION}/boltz-client-${DL_SUFFIX}-${BOLTZ_VERSION}.tar.gz" \
    && tar -xzf /tmp/boltz-client.tar.gz -C /tmp \
    && install -m 0755 /tmp/bin/${TAR_DIR}/boltzd /usr/local/bin/boltzd \
    && install -m 0755 /tmp/bin/${TAR_DIR}/boltzcli /usr/local/bin/boltzcli \
    && rm -rf /tmp/boltz-client.tar.gz /tmp/bin

# =============================================================================
# C-LIGHTNING-REST (for RTL integration)
# =============================================================================
# REST API plugin enabling Ride The Lightning web interface

ARG CLN_REST_VERSION=v0.10.7
RUN git clone --depth 1 --branch ${CLN_REST_VERSION} https://github.com/Ride-The-Lightning/c-lightning-REST.git /opt/c-lightning-REST \
    && cd /opt/c-lightning-REST \
    && npm install --omit=dev \
    && chmod +x cl-rest.js

# =============================================================================
# CL-REVENUE-OPS PLUGIN
# =============================================================================
# Fallback: Clone from GitHub if not mounted from host.
# For hot upgrades, mount from host in docker-compose.yml:
#   - /path/to/cl_revenue_ops:/opt/cl-revenue-ops:ro

ARG CL_REVENUE_OPS_VERSION=v2.2.5
RUN git clone --depth 1 --branch ${CL_REVENUE_OPS_VERSION} https://github.com/lightning-goats/cl_revenue_ops.git /opt/cl-revenue-ops \
    && chmod +x /opt/cl-revenue-ops/cl-revenue-ops.py

# =============================================================================
# OPTIONAL PHASE 6 PLUGINS (disabled by default at runtime)
# =============================================================================
# These repos are baked into the image so operators can enable them via:
#   HIVE_COMMS_ENABLED=true
#   HIVE_ARCHON_ENABLED=true

ARG CL_HIVE_COMMS_VERSION=main
RUN git clone --depth 1 --branch ${CL_HIVE_COMMS_VERSION} https://github.com/lightning-goats/cl-hive-comms.git /opt/cl-hive-comms \
    && chmod +x /opt/cl-hive-comms/cl-hive-comms.py

ARG CL_HIVE_ARCHON_VERSION=main
RUN git clone --depth 1 --branch ${CL_HIVE_ARCHON_VERSION} https://github.com/lightning-goats/cl-hive-archon.git /opt/cl-hive-archon \
    && chmod +x /opt/cl-hive-archon/cl-hive-archon.py

# =============================================================================
# CL-HIVE PLUGIN
# =============================================================================
# Fallback: Copy files if not mounted from host.
# For hot upgrades, mount from host in docker-compose.yml:
#   - /path/to/cl-hive:/opt/cl-hive:ro
# When mounted, the host's git repo enables ./scripts/hot-upgrade.sh

COPY . /opt/cl-hive
RUN chmod +x /opt/cl-hive/cl-hive.py

# =============================================================================
# TOR CONFIGURATION
# =============================================================================

RUN mkdir -p /var/lib/tor/cln-service \
    && chown -R debian-tor:debian-tor /var/lib/tor

COPY docker/torrc /etc/tor/torrc

# =============================================================================
# DIRECTORY STRUCTURE
# =============================================================================

# Create lightning user
RUN useradd -m -s /bin/bash lightning

RUN mkdir -p /home/lightning/.lightning/bitcoin \
    && mkdir -p /home/lightning/.lightning/plugins \
    && mkdir -p /data/lightning \
    && mkdir -p /data/bitcoin \
    && chown -R lightning:lightning /home/lightning /data

# Symlink plugins to CLN plugins directory
RUN ln -sf /opt/cl-hive/cl-hive.py /home/lightning/.lightning/plugins/cl-hive.py \
    && ln -sf /opt/cl-hive/modules /home/lightning/.lightning/plugins/modules \
    && ln -sf /opt/cl-revenue-ops/cl-revenue-ops.py /home/lightning/.lightning/plugins/cl-revenue-ops.py \
    && ln -sf /opt/cl-revenue-ops/modules /home/lightning/.lightning/plugins/revenue-modules \
    && ln -sf /usr/local/bin/clboss /home/lightning/.lightning/plugins/clboss \
    && ln -sf /usr/local/bin/vitality /home/lightning/.lightning/plugins/vitality \
    && ln -sf /usr/local/bin/sling /home/lightning/.lightning/plugins/sling \
    && ln -sf /opt/c-lightning-REST/cl-rest.js /home/lightning/.lightning/plugins/cl-rest.js

# =============================================================================
# CONFIGURATION FILES
# =============================================================================

COPY docker/docker-entrypoint.sh /usr/local/bin/
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY docker/lightning.conf.template /etc/lightning/lightning.conf.template
COPY cl-hive.conf.sample /etc/lightning/cl-hive.conf

# Copy shutdown and utility scripts
COPY docker/scripts/pre-stop.sh /usr/local/bin/
COPY docker/scripts/lightningd-wrapper.sh /usr/local/bin/
COPY docker/scripts/emergency-recover-watcher.sh /usr/local/bin/
COPY docker/scripts/plugin-db-backup.sh /usr/local/bin/

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    /usr/local/bin/pre-stop.sh \
    /usr/local/bin/lightningd-wrapper.sh \
    /usr/local/bin/emergency-recover-watcher.sh \
    /usr/local/bin/plugin-db-backup.sh

# =============================================================================
# PORTS
# =============================================================================

# Lightning P2P
EXPOSE 9736
# c-lightning-REST API (for RTL)
EXPOSE 3001
# Tor SOCKS (internal)
EXPOSE 9050
# WireGuard
EXPOSE 51820/udp

# =============================================================================
# VOLUMES
# =============================================================================

VOLUME ["/data/lightning", "/data/bitcoin", "/etc/wireguard"]

# =============================================================================
# ENTRYPOINT
# =============================================================================

WORKDIR /root

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
