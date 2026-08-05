# The pipeline's runtime, baked.
#
# Everything that takes time lives in `bootstrap.sh`, which also runs as a
# job's first command on a stock image.  Two routes to the same runtime: bake
# this when we have a registry to push to, run the script when we do not.
#
# Ubuntu 22.04 rather than a `python:` image: WPS 11.1.0.11723 is a 2023-era
# package built against that generation of glibc and GTK, and 22.04 already
# carries Python 3.10, which is what `pyproject.toml` asks for.
FROM ubuntu:22.04

COPY bootstrap.sh /tmp/bootstrap.sh
RUN bash /tmp/bootstrap.sh && rm -f /tmp/bootstrap.sh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# The pipeline itself is mounted or cloned at run time rather than copied in.
# It changes every hour; the 1.7 GB underneath it does not, and rebuilding
# that layer to pick up a one-line fix is the slowest possible way to iterate.
WORKDIR /work

CMD ["/bin/bash"]
