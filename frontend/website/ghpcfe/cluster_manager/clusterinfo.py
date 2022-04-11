#!/usr/bin/env python3
# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# To create a cluster, we need:
# 1) Know which Cloud Provider & region/zone/project
# 2) Know authentication credentials
# 3) Know an "ID Number" or name - for directory to store state info

# 1 - Supplied via commandline
# 2 - Supplied via... Env vars / commandline?
# 3 - Supplied via commandline



import argparse
from pathlib import Path
import os, shutil, sys
import subprocess
import json

from . import utils
from . import cloud_info
from . import c2

from google.api_core.exceptions import PermissionDenied as GCPPermissionDenied

from django.template import engines as template_engines

import logging
logger = logging.getLogger(__name__)


from ..models import Cluster, ApplicationInstallationLocation, \
    ComputeInstance, InstanceType

class ClusterInfo:
    """ Expected process:

ClusterInfo object - represent a cluster
- Call prepare()
    - This will create the directory, dump a YAML file for GHPC
- Call update()
    - This will dump a new YAML file
- Call start_cluster()
    - Calls ghpc to create Terraform
    - Initializes Terraform
    - Applies Terraform
"""

    def __init__(self, cluster):
        self.config = utils.load_config()
        self.ghpc_path = self.config["baseDir"].parent / 'ghpc'

        self.cluster = cluster
        self.cluster_dir = self.config["baseDir"] / 'clusters' / f'cluster_{self.cluster.id}'

    def prepare(self, credentials):
        self._create_cluster_dir()
        self._set_credentials(credentials)
        self.update()

    def update(self):
        self._prepare_ghpc_yaml()
        self._prepare_bootstrap_gcs()

    def start_cluster(self):
        self.cluster.cloud_state = 'nm'
        self.cluster.status = 'c'
        self.cluster.save()

        try:
            self._run_ghpc()
            self._initialize_terraform()
            self._apply_terraform()
        except Exception as e:
            self.cluster.status = 'e'
            self.cluster.cloud_status = 'nm'
            self.cluster.save()
            raise

    def stop_cluster(self):
        self._destroy_terraform()

    def get_cluster_access_key(self):
        return self.cluster.get_access_key()



    def _create_cluster_dir(self):
        self.cluster_dir.mkdir(parents=True)

    def _get_credentials_file(self):
        return self.cluster_dir / 'cloud_credentials'

    def _set_credentials(self, creds=None):
        credfile = self._get_credentials_file()
        if not creds:
            # pull from DB
            creds = self.cluster.cloud_credential.detail
        with credfile.open('w') as fp:
                fp.write(creds)

        # Create SSH Keys
        self._create_ssh_key(self.cluster_dir)


    def _create_ssh_key(self, tgtDir):
        #ssh-keygen -t rsa -f <tgtdir>/.ssh/id_rsa -N ""
        sshdir = tgtDir / '.ssh'
        sshdir.mkdir(mode = 0o711)

        priv_key_file = sshdir / 'id_rsa'
        pub_key_file = sshdir / 'id_rsa.pub'

        subprocess.run(["ssh-keygen",
                    "-t", "rsa",
                    "-f", priv_key_file.as_posix(),
                    "-N", "",
                    "-C", "citc@mgmt"],
                    check=True)




    def _prepare_ghpc_filesystems(self):
        yaml = []
        refs = []
        for (count, mp) in enumerate(self.cluster.mount_points.order_by('mount_order')):
            storage_id = f"mount_num_{count}"
            ip = "'$controller'" if mp.export in self.cluster.shared_fs.exports.all() else mp.export.server_name
            yaml.append(f"""
  - source: resources/file-system/pre-existing-network-storage
    kind: terraform
    id: {storage_id}
    settings:
      server_ip: {ip}
      remote_mount: {mp.export.export_name}
      local_mount: {mp.mount_path}
      mount_options: {mp.mount_options}
      fs_type: {mp.fstype_name}
""")
            refs.append(storage_id)

        return ("\n\n".join(yaml), refs)

    def _prepare_ghpc_partitions(self, part_uses):
        # TODO:  Eventually should have actual "partitions" in the Model, with a name, etc
        yaml = []
        refs = []
        uses_str = self._yaml_refs_to_uses(part_uses)
        for (count, part) in enumerate(self.cluster.partitions.all()):
            part_id = f"partition_{count}"
            image_str = f"image: {part.image}\n" if part.image else ""
            yaml.append(f"""
  - source: resources/third-party/compute/SchedMD-slurm-on-gcp-partition
    kind: terraform
    id: {part_id}
    settings:
      partition_name: {part.name}
      subnetwork_name: {self.cluster.subnet.cloud_id}
      max_node_count: {part.max_node_count}
      machine_type: {part.machine_type.name}
      enable_placement: {part.enable_placement}
      image_hyperthreads: {part.enable_hyperthreads}
      exclusive: {part.enable_placement or not part.enable_node_reuse}
      {image_str}
    use:
{uses_str}
""")
            if part.image:
                yaml.append("      image: {part.image}\n")

            refs.append(part_id)

        return ("\n\n".join(yaml), refs)

    def _yaml_refs_to_uses(self, use_list):
        return "\n".join([f"    - {x}" for x in use_list])


    def _prepare_ghpc_yaml(self):
        ymlFile = self.cluster_dir / 'cluster.yaml'
        project_id = json.loads(self.cluster.cloud_credential.detail)["project_id"]

        (filesystems_yaml, filesystems_references) = self._prepare_ghpc_filesystems()
        (partitions_yaml, partitions_references) = self._prepare_ghpc_partitions(["hpc_network"] + filesystems_references)

        controller_uses = self._yaml_refs_to_uses(["hpc_network"] + partitions_references + filesystems_references)
        login_uses = self._yaml_refs_to_uses(["hpc_network"] + filesystems_references + ["slurm_controller"])

        controller_sa = f"{self.cluster.cloud_id}-sa"
        # TODO: Determine if these all should be different, and if so, add to resource to be created
        # Note though, that at the moment, GHPC won't let us unpack output
        # variables, so we can't index properly.
        # for now, just use the singular access, and only create a single acct
        #compute_sa = controller_sa
        #login_sa = controller_sa

        startup_bucket = self.config["server"]["gcs_bucket"]
        with ymlFile.open('w') as f:
            f.write(f"""
blueprint_name: {self.cluster.cloud_id}

vars:
  project_id: {project_id}
  deployment_name: {self.cluster.cloud_id}
  region: {self.cluster.cloud_region}
  zone: {self.cluster.cloud_zone}

resource_groups:
- group: primary
  resources:
  - source: resources/network/pre-existing-vpc
    kind: terraform
    settings:
      network_name: {self.cluster.subnet.vpc.cloud_id}
      subnetwork_name: {self.cluster.subnet.cloud_id}
    id: hpc_network

{filesystems_yaml}

  - source: resources/project/service-account
    kind: terraform
    id: hpc_service_account
    settings:
      project_id: {project_id}
      names: [ {controller_sa} ]
      project_roles:
      - compute.instanceAdmin.v1
      - iam.serviceAccountUser
      - monitoring.metricWriter
      - logging.logWriter
      - storage.objectAdmin
      - pubsub.publisher
      - pubsub.subscriber

{partitions_yaml}

  - source: resources/third-party/scheduler/SchedMD-slurm-on-gcp-controller
    kind: terraform
    id: slurm_controller
    settings:
      login_node_count: {self.cluster.num_login_nodes}
      controller_service_account: $(hpc_service_account.email)
      controller_startup_script: |
        #!/bin/bash
        echo "******************************************** CALLING CONTROLLER STARTUP"
        gsutil cp gs://{startup_bucket}/clusters/{self.cluster.id}/bootstrap_controller.sh - | bash
      compute_node_service_account: $(hpc_service_account.email)
      compute_node_scopes:
      - https://www.googleapis.com/auth/monitoring.write
      - https://www.googleapis.com/auth/logging.write
      - https://www.googleapis.com/auth/devstorage.read_write
      - https://www.googleapis.com/auth/pubsub
      compute_startup_script: |
        #!/bin/bash
        gsutil cp gs://{startup_bucket}/clusters/{self.cluster.id}/bootstrap_compute.sh - | bash 
    use:
{controller_uses}

  - source: resources/third-party/scheduler/SchedMD-slurm-on-gcp-login-node
    kind: terraform
    id: slurm_login
    settings:
      login_node_count: {self.cluster.num_login_nodes}
      subnetwork_name: {self.cluster.subnet.cloud_id}
      login_service_account: $(hpc_service_account.email)
      login_scopes:
      - https://www.googleapis.com/auth/monitoring.write
      - https://www.googleapis.com/auth/logging.write
      - https://www.googleapis.com/auth/devstorage.read_write
      login_startup_script: |
        #!/bin/bash
        echo "******************************************** CALLING LOGIN STARTUP"
        gsutil cp gs://{startup_bucket}/clusters/{self.cluster.id}/bootstrap_login.sh - | bash
    use:
{login_uses}

""")

    def _prepare_bootstrap_gcs(self):
        template_dir = self.config["baseDir"] / 'infrastructure_files' / 'cluster_startup' / 'templates'
        engine = template_engines['django']
        for templ in ["controller", "login", "compute"]:
            template_fn = template_dir / f"bootstrap_{templ}.sh"
            with open(template_fn, 'r') as fp:
                tstr = fp.read()
                template = engine.from_string(tstr)
                # TODO: Add to context any other things we may need in the startup script
                rendered_file = template.render(context={
                    'server_bucket': self.config["server"]["gcs_bucket"],
                    'cluster': self.cluster,
                    'spack_dir': self.cluster.spackdir,
                    'fec2_topic': c2.get_topic_path(),
                    'fec2_subscription': c2.get_cluster_subscription_path(self.cluster.id),
                    })
                blobpath = f"clusters/{self.cluster.id}/{template_fn.name}"
                cloud_info.gcs_upload_file(self.config["server"]["gcs_bucket"], blobpath, rendered_file)


    def _initialize_terraform(self):
        tfDir = self.get_terraform_dir()
        extraEnv = {'GOOGLE_APPLICATION_CREDENTIALS': self._get_credentials_file()}
        try:
            logger.info("Invoking Terraform Init")
            utils.run_terraform(tfDir, "init")
            utils.run_terraform(tfDir, "validate", extraEnv=extraEnv)
            logger.info("Invoking Terraform Plan")
            utils.run_terraform(tfDir, "plan", extraEnv=extraEnv)
        except subprocess.CalledProcessError as cpe:
            logger.error("Terraform exec failed", exc_info=cpe)
            if cpe.stdout:
                logger.info(f"  STDOUT:\n{cpe.stdout.decode('utf-8')}\n")
            if cpe.stderr:
                logger.info(f"  STDERR:\n{cpe.stderr.decode('utf-8')}\n")
            raise

    def _run_ghpc(self):
        tgtDir = self.cluster_dir
        try:
            logger.info("Invoking ghpc create")
            log_out_fn = tgtDir / f"ghpc_create_log.stdout"
            log_err_fn = tgtDir / f"ghpc_create_log.stderr"
            with log_out_fn.open('wb') as log_out:
                with log_err_fn.open('wb') as log_err:
                    subprocess.run([self.ghpc_path.as_posix(), 'create', 'cluster.yaml'],
                        cwd=tgtDir,
                        stdout=log_out, stderr=log_err,
                        check=True)
        except subprocess.CalledProcessError as cpe:
            logger.error("ghpc exec failed", exc_info=cpe)
            # No logs from stdout/err - get dumped to files
            raise


    def _get_tf_state_resource(self, state, filters):
        """Given a Terraform State json file, look for the Resource that matches each entry
        in the supplied filters dictionary.  Returns each match"""
        def matches(x):
            try:
                for k,v in filters.items():
                    if x[k] != v:
                        return False
                return True
            except KeyError:
                return False
        return [x for x in filter(matches, state["resources"])]

    def _create_model_instances_from_tf_state(self, state, filters):
        tf_nodes = self._get_tf_state_resource(state, filters)[0]["instances"]
        def model_from_tf(tf):
            ciKwargs = {
                'id': None,
                'cloud_credential': self.cluster.cloud_credential,
                'cloud_state': 'm',
                'cloud_region': self.cluster.cloud_region,
                'cloud_zone': self.cluster.cloud_zone,
            }

            try:
                ciKwargs['cloud_id'] = tf["attributes"]["name"]
                instance_type_name = tf["attributes"]["machine_type"]
                ciKwargs['instance_type'] = InstanceType.objects.get(name=instance_type_name)
            except (KeyError):
                pass

            try:
                nic = tf["attributes"]["network_interface"][0]
                ciKwargs['internal_ip'] = nic["network_ip"]
                ciKwargs['public_ip'] = nic["access_config"][0]["nat_ip"]
            except (KeyError, IndexError):
                pass

            try:
                service_acct = tf["attributes"]["service_account"][0]
                ciKwargs['service_account'] = service_acct["email"]
            except (KeyError, IndexError):
                pass

            return ComputeInstance(**ciKwargs)
        return [model_from_tf(instance) for instance in tf_nodes]

    def _get_service_accounts(self, tf_state):
# TODO:  Once we're creating service accounts, can pull them from those resources
# At the moment, pull from controller & login instances. This misses "compute"
# nodes, but they're going to just be the same as controller & login until
# we start setting them.
        #filters = {'module': "module.hpc_service_account.module.service_accounts",
        #           'name': 'service_accounts'}
        #sa_accounts = self._get_tf_state_resource(tf_state, filters)[0]["instances"]
        #ctrl_sa = sa_accounts[*]['attributes']['email'] where * defined by attributes-account_id

        filters = {'module': "module.slurm_controller.module.slurm_cluster_controller",
                   'name': 'controller_node'}
        tf_node = self._get_tf_state_resource(tf_state, filters)[0]["instances"][0]
        ctrl_sa = tf_node["attributes"]["service_account"][0]["email"]

        filters = {'module': "module.slurm_login.module.slurm_cluster_login_node",
                   'name': 'login_node'}
        tf_node = self._get_tf_state_resource(tf_state, filters)[0]["instances"][0]
        login_sa = tf_node["attributes"]["service_account"][0]["email"]

        return {'controller': ctrl_sa,
                'login': login_sa,
                'compute': login_sa}


    def _apply_service_account_permissions(self, service_accounts):
        # Need to give permission for all instances to download startup scripts
        # Need to give ability to upload job log files to bucket
        # TODO:  Figure out who exactly will do this.  For now, grant to all.
        all_sas = set(service_accounts.values())
        bucket = self.config["server"]["gcs_bucket"]
        for sa in all_sas:
            cloud_info.gcs_apply_bucket_acl(bucket, f'serviceAccount:{sa}', permission='roles/storage.objectAdmin')

        # Give Command & Control access
        try:
            c2.add_cluster_subscription_service_account(self.cluster.id, service_accounts['controller'])
        except GCPPermissionDenied:
            logger.warning("Permission Denied attempting to add IAM permissions for service account to PubSub Subscription/Topic.  Command and Control may not work.  Please grant the role of pubsub.admin to FrontEnd service account.")
            if self.cluster.project_id != self.config["server"]["gcp_project"]:
                logger.error("Cluster project differs from FrontEnd project.  C&C will not work.")

    def _apply_terraform(self):
        tfDir = self.get_terraform_dir()

        # Create C&C Subscription
        c2.create_cluster_subscription(self.cluster.id)


        extraEnv = {'GOOGLE_APPLICATION_CREDENTIALS': self._get_credentials_file()}
        try:
            logger.info("Invoking Terraform Apply")
            (log_out, log_err) = utils.run_terraform(tfDir, "apply", extraEnv=extraEnv)

            # Look for Management and Login Nodes in TF state file
            stateFile = tfDir / 'terraform.tfstate'
            with stateFile.open('r') as statefp:
                state = json.load(statefp)

                # Apply Perms to the service accounts
                service_accounts = self._get_service_accounts(state)
                self._apply_service_account_permissions(service_accounts)

                # Cluster is now being initialized
                self.cluster.internal_name = self.cluster.name
                self.cluster.cloud_state = 'm'

                # Cluster initialization is now running.
                self.cluster.status = 'i'
                self.cluster.save()

                mgmtNodes = self._create_model_instances_from_tf_state(state, {
                    'module': "module.slurm_controller.module.slurm_cluster_controller",
                    "name": "controller_node"})
                if len(mgmtNodes) != 1:
                    logger.warning(f"Found {len(mgmtNodes)} management nodes, rather than the 1 expected!")
                if len(mgmtNodes):
                    node = mgmtNodes[0]
                    node.save()
                    utils.add_host_to_server_firewall(node.public_ip)
                    self.cluster.controller_node = node
                    logger.info(f"Created cluster at (mgmt) {node.public_ip if node.public_ip else node.internal_ip}")


                loginNodes = self._create_model_instances_from_tf_state(state, {
                    'module': "module.slurm_login.module.slurm_cluster_login_node",
                    "name": "login_node"})
                if len(loginNodes) != self.cluster.num_login_nodes:
                    logger.warning(f"Found {len(loginNodes)} login nodes, rather than the {self.cluster.num_login_nodes} expected!")
                for lnode in loginNodes:
                    lnode.cluster_login = self.cluster
                    lnode.save()
                    utils.add_host_to_server_firewall(lnode.public_ip)
                    logger.info(f"Created login at {lnode.public_ip if lnode.public_ip else lnode.internal_ip}")

                # Set up Spack Install location
                self._configure_spack_install_loc()

                self.cluster.save()


        except subprocess.CalledProcessError as cpe:
            # We can error during provisioning, in which case Terraform
            # doesn't tear things down.  Run a `destroy`, just in case
            self._destroy_terraform()
            raise


    def _destroy_terraform(self):
        tfDir = self.get_terraform_dir()
        extraEnv = {'GOOGLE_APPLICATION_CREDENTIALS': self._get_credentials_file()}
        try:
            logger.info("Invoking Terraform destroy")
            self.cluster.status = 't'
            self.cluster.cloud_status = 'dm'
            self.cluster.save()

            #utils.remove_host_from_server_firewall(self.cluster.XXXX)
            utils.run_terraform(tfDir, 'destroy', extraEnv=extraEnv)

            controller_sa = self.cluster.controller_node.service_account

            self.cluster.controller_node.delete()
            self.cluster.login_nodes.all().delete()
            # Refresh so our python object gets the SET_NULL's from the above deletes
            self.cluster = Cluster.objects.get(id=self.cluster.id)

            self.cluster.status = 'd'
            self.cluster.cloud_status = 'xm'
            self.cluster.save()

            c2.delete_cluster_subscription(self.cluster.id, controller_sa)
        except subprocess.CalledProcessError as cpe:
            logger.error("Terraform destroy failed", exc_info=cpe)
            if cpe.stdout:
                logger.info(f"  STDOUT:\n{cpe.stdout.decode('utf-8')}\n")
            if cpe.stderr:
                logger.info(f"  STDERR:\n{cpe.stderr.decode('utf-8')}\n")
            raise



    def _configure_spack_install_loc(self):
        """Configures the spack_install field.
        Could point to an existing install, if paths match appropriately,
        otherwise, create a new DB entry.
        """
        cluster_spack_dir = self.cluster.spackdir
        # Find the mount point that best matches our spack dir
        spack_mp = None
        for mp in self.cluster.mount_points.order_by('mount_order'):
            if cluster_spack_dir.startswith(mp.mount_path):
                spack_mp = mp

        if not spack_mp:
            logger.error(f"Unable to find a mount_point matching out spack path {cluster_spack_dir}")
            return

        partial_path = cluster_spack_dir[len(spack_mp.mount_path)+1:]
        # Now we have a Mount Point, Find app Install locs with that MP's export
        possible_apps = ApplicationInstallationLocation.objects.filter(fs_export=spack_mp.export).filter(path=partial_path)
        if possible_apps:
            self.cluster.spack_install = possible_apps[0]
        else:
            # Need to create a new entry
            self.cluster.spack_install = ApplicationInstallationLocation(fs_export=spack_mp.export, path=partial_path)
            self.cluster.spack_install.save()
        self.cluster.save()

    def get_app_install_loc(self, install_path):
        my_mp = None
        for mp in self.cluster.mount_points.order_by('mount_order'):
            if install_path.startswith(mp.mount_path):
                my_mp = mp

        if not my_mp:
            logger.warning(f"Unable to find a mount_point matching path {install_path}")
            return None

        partial_path = install_path[len(my_mp.mount_path)+1:]
        possible_apps = ApplicationInstallationLocation.objects.filter(fs_export=my_mp.export).filter(path=partial_path)
        if possible_apps:
            return possible_apps[0]
        else:
            # Need to create a new entry
            install_loc = ApplicationInstallationLocation(fs_export=my_mp.export, path=partial_path)
            install_loc.save()
            return install_loc



    def get_terraform_dir(self):
        return self.cluster_dir / self.cluster.cloud_id / 'primary'
