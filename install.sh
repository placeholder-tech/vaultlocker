#!/bin/bash
source venv/bin/activate 
pip install -e .

CWD=`pwd`

cat << EOF > /usr/bin/vaultlocker
#!/bin/bash
source /${CWD}/venv/bin/activate
vaultlocker \$*
EOF

chmod 700 /usr/bin/vaultlocker
