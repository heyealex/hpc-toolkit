if [ ! -d /tmp/jupyterhome\home ]; then ln -s /home /tmp/jupyterhome/; fi

echo "modifying jupyter config" | tee -a /tmp/startup.log
echo "jupyter_user = \"$USER\"" >> /tmp/jupyterhome/.jupyter/jupyter_notebook_config.py
echo "jupyter_home = \"/tmp/jupyterhome\"" >> /tmp/jupyterhome/.jupyter/jupyter_notebook_config.py
echo 'sys.path.append(f"{jupyter_home}/.jupyter/")' >> /tmp/jupyterhome/.jupyter/jupyter_notebook_config.py
echo "c.ServerApp.notebook_dir = \"/tmp/jupyterhome\"" >> /tmp/jupyterhome/.jupyter/jupyter_notebook_config.py

echo "modifying jupyter service" | tee -a /tmp/startup.log
cat > /lib/systemd/system/jupyter.service <<+ 
[Unit]
Description=Jupyter Notebook Service

[Service]
Type=simple
PIDFile=/run/jupyter.pid
MemoryHigh=3493718272
MemoryMax=3543718272
ExecStart=/bin/bash --login -c '/opt/conda/bin/jupyter lab --config=/tmp/jupyterhome/.jupyter/jupyter_notebook_config.py'
#User=jupyter
User=$USER
Group=$USER
WorkingDirectory=/tmp/jupyterhome
Restart=always

[Install]
WantedBy=multi-user.target
+

echo "reloading and restarting service" | tee -a /tmp/startup.log
systemctl daemon-reload
service jupyter restart