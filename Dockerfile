FROM rockylinux:9
RUN dnf install -y epel-release && dnf install -y gcc python3.11 python3.11-devel patchelf git && dnf clean all