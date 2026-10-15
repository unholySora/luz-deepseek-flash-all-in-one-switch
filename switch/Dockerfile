# Run docker build from the REPOSITORY ROOT, not the switch directory.
ARG BASE_IMAGE=luz-base:0.3.1-r3
FROM ${BASE_IMAGE}
COPY deploy/adapter /opt/dsv41/adapter
COPY deploy/b12x /opt/b12x-tree
COPY deploy/overlay/o12c1-prod /opt/dsv41/overlay
COPY switch/image-context/_roce_proxy.c /opt/b12x-tree/b12x/comm/roce_ring/_roce_proxy.c
COPY switch/tools/install_overlay.py switch/tools/pack_engram.py switch/tools/sanitize_image.py /opt/dsv41/site/
RUN python3 /opt/dsv41/site/install_overlay.py && \
    gcc -O3 -std=c11 -shared -fPIC -pthread /opt/b12x-tree/b12x/comm/roce_ring/_roce_proxy.c -libverbs -o /tmp/roce-switch-check.so && \
    rm /tmp/roce-switch-check.so && \
    python3 /opt/dsv41/site/sanitize_image.py
ENV PYTHONPATH=/opt/dsv41/adapter:/opt/b12x-tree:/sgl-workspace/sglang/python \
    LD_LIBRARY_PATH=/opt/sglang/lib/python3.12/site-packages/nvidia/nccl/lib:/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    LD_PRELOAD=""
LABEL luz.variant="switch-dual100g" luz.source_base="fb5798c6b73d543b297bc3ff410003549f5d96da"
