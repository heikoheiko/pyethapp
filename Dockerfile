## Builds pyethapp from github in a python 2.7 docker container.
##
## Build via:
#
#  `docker build --rm --no-cache -t pyethapp:latest
#
## the '--rm --no-cache' above avoids stacking up and or reusing intermediary builds.
##
## Run via:
# 
# `docker run --rm \
#  -p 0.0.0.0:30303:30303 \
#  -p 0.0.0.0:30303:30303/udp \
#  -v <host dir>:/data \
#  pyethapp:latest`
#
## Note, that <host dir> given in '-v <host dir>:/data' above needs
## to be writable for the user inside the container (uid:1000/gid:1000).

FROM python:2.7.9

RUN apt-get update && \
    apt-get install -y git-core && \
    apt-get clean

RUN git clone https://github.com/ethereum/pyrlp && cd pyrlp &&\
    git checkout develop && \
    pip install -r requirements.txt && \
    python setup.py install

RUN git clone https://github.com/ethereum/pydevp2p && cd pydevp2p &&\
    pip install -r requirements.txt && \
    python setup.py install

RUN git clone https://github.com/ethereum/pyethereum && cd pyethereum &&\
    git checkout develop && \
    pip install -r requirements.txt && \
    python setup.py install

RUN git clone https://github.com/ethereum/pyethapp && cd pyethapp && \
    python setup.py install

# Fix debian's ridicolous gevent-breaking constant removal
# (e.g. https://github.com/hypothesis/h/issues/1704#issuecomment-63893295):
RUN sed -i 's/PROTOCOL_SSLv3/PROTOCOL_SSLv23/g' /usr/local/lib/python2.7/site-packages/gevent/ssl.py

# For docker builds from git, we add the last commit to the version string:
RUN sed -i "s/client_version = 'pyethapp/client_version = 'pyethapp_$(git rev-parse HEAD| cut -c 1-6)/g" /usr/local/lib/python2.7/site-packages/pyethapp*/pyethapp/app.py

RUN adduser pyethuser

# We use a startscript to wrap the '-d /data' parameter.
# in theory this wouldn't be necessary and we could use the following two lines:
#ENTRYPOINT ["/usr/local/bin/pyethapp", "-d", "/data/pyethdata"]
#CMD ["run"]
# unfortunately, in practice it doesn't though.
RUN echo '#!/bin/bash\nset -e\n\nexec /usr/local/bin/pyethapp -d /data/pyethdata "$@"' > /home/pyethuser/startscript.sh

RUN chmod +x /home/pyethuser/startscript.sh &&\
    chown -R pyethuser. /home/pyethuser
    
USER pyethuser

ENTRYPOINT ["/home/pyethuser/startscript.sh"]
CMD ["run"]

# mount host directory via "-v /path/to/host/dir:/data" - needs u+w or g+w for 1000
VOLUME ["/data"]
