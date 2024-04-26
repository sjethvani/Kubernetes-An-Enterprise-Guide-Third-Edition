import base64
import pulumi
from pulumi_kubernetes import helm, Provider
import pulumi_kubernetes as k8s
from pulumi_kubernetes.apiextensions.CustomResource import CustomResource
from ...lib.helm_chart_versions import get_latest_helm_chart_version
import logging
import kubernetes
from kubernetes import config as kube_config, dynamic
from kubernetes import client as k8s_client
from kubernetes.dynamic.exceptions import ResourceNotFoundError
from kubernetes.client import api_client
import secrets

def load_ca_cert():
    # this is probably the wrong way to do this, but <shrug>
    k8s_cp_api = kube_config.kube_config.new_client_from_config(pulumi.Config().require("kube.cp.path"))
    k8s_cp_core_api = k8s_client.CoreV1Api(k8s_cp_api)
    k8s_cp_custom_api = k8s_client.CustomObjectsApi(k8s_cp_api)

    cluster_issuer_object = k8s_cp_custom_api.get_cluster_custom_object(group="cert-manager.io",version="v1",plural="clusterissuers",name="enterprise-ca")
    cluster_issuer_ca_secret_name = cluster_issuer_object["spec"]["ca"]["secretName"]
    pulumi.log.info("Loading CA from {}".format(cluster_issuer_ca_secret_name))
    ca_secret = k8s_cp_core_api.read_namespaced_secret(namespace="cert-manager",name=cluster_issuer_ca_secret_name)
    ca_cert = ca_secret.data["tls.crt"]

    return ca_cert


def load_oidc_secret(k8s_provider):
    # this is probably the wrong way to do this, but <shrug>
    k8s_cp_api = kube_config.kube_config.new_client_from_config(pulumi.Config().require("kube.cp.path"))
    k8s_cp_core_api = k8s_client.CoreV1Api(k8s_cp_api)
    

    try:
        oidc_secret =k8s_cp_core_api.read_namespaced_secret(namespace="openunison",name="gitlab-oidc")
        oidc_client_secret = oidc_secret.data["gitlab.oidc.client_secret"]

        decoded_secret = base64.b64decode(oidc_client_secret).decode("utf-8")

    except kubernetes.client.exceptions.ApiException:
        # the secret doesn't exist, let's create it
        decoded_secret = secrets.token_urlsafe(64)
    
    gitlab_oidc_secret = k8s.core.v1.Secret(
    "gitlab-oidc",
    metadata= k8s.meta.v1.ObjectMetaArgs(
        name="gitlab-oidc",
        namespace="openunison"
    ),
    data={
        "gitlab.oidc.client_secret": base64.b64encode(decoded_secret.encode('utf-8')).decode('utf-8'),
    },
    opts=pulumi.ResourceOptions(
        provider = k8s_provider,
        retain_on_delete=False,
        delete_before_replace=True,
        custom_timeouts=pulumi.CustomTimeouts(
            create="10m",
            update="10m",
            delete="10m"
        )
    )
    )

    return [gitlab_oidc_secret,decoded_secret]




def deploy_gitlab(name: str, k8s_provider: Provider, kubernetes_distribution: str, project_name: str, namespace: str,openunison_cluster_management_release):
    logging.info("in deploy_gitlab")
    config = pulumi.Config()
    # Create a Namespace
    gitlab_namespace = k8s.core.v1.Namespace("gitlab_namespace",
        metadata= k8s.meta.v1.ObjectMetaArgs(
            name="gitlab"
        ),
        opts=pulumi.ResourceOptions(
            provider = k8s_provider,
            retain_on_delete=True,
            custom_timeouts=pulumi.CustomTimeouts(
                create="10m",
                update="10m",
                delete="10m"
            )
        )
    )

    # Create GitLab SSO secret
    
    
    # load our CA cert
    ca_cert = load_ca_cert()

    # get the domain suffix and cluster_issuer
    domain_suffix = config.require('openunison.cp.dns_suffix')

    # get the oidc client secret
    [gitlab_oidc_secret,oidc_client_secret] = load_oidc_secret(k8s_provider)

    sso_secret = """name: openid_connect
label: OpenUnison
args:
  name: openid_connect
  scope:
    - openid
    - profile
  response_type: code
  issuer: https://k8sou.""" + domain_suffix + """/auth/idp/k8sIdp
  discovery: true
  client_auth_method: query
  uid_field: sub
  send_scope_to_token_endpoint: false
  pkce: true
  client_options:
    identifier: gitlab
    secret: """ + oidc_client_secret + """
    redirect_uri: https://gitlab.""" + domain_suffix + """/users/auth/openid_connect/callback"""



    gitlab_oidc_config_secret = k8s.core.v1.Secret(
    "gitlab-oidc-cfg",
    metadata= k8s.meta.v1.ObjectMetaArgs(
        name="gitlab-oidc",
        namespace="gitlab"
    ),
    data={
        "provider": base64.b64encode(sso_secret.encode('utf-8')).decode('utf-8'),
    },
    opts=pulumi.ResourceOptions(
        provider = k8s_provider,
        depends_on=[gitlab_oidc_secret],
        retain_on_delete=False,
        delete_before_replace=True,
        custom_timeouts=pulumi.CustomTimeouts(
            create="10m",
            update="10m",
            delete="10m"
        )
    )
    )

    # create a secret that will store our CA cert for gitlab to trust

    internal_ca_secret = k8s.core.v1.Secret(
    "internal-ca-secret",
    metadata= k8s.meta.v1.ObjectMetaArgs(
        name="internal-ca",
        namespace="gitlab"
    ),
    data={
        "tls.crt": load_ca_cert(),
        domain_suffix:load_ca_cert(),
        "gitlab." + domain_suffix: load_ca_cert()
    },
    opts=pulumi.ResourceOptions(
        provider = k8s_provider,
        retain_on_delete=False,
        delete_before_replace=True,
        custom_timeouts=pulumi.CustomTimeouts(
            create="10m",
            update="10m",
            delete="10m"
        )
    )
    )

    gitlab_wildcard_certificate = CustomResource(
        "gitlab_wildcard_certificate",
        api_version="cert-manager.io/v1",
        kind="Certificate",
        metadata={
            "name": "gitlab-wildcard-certificate",
            "namespace": "gitlab"
        },
        spec={
            "commonName": "*." + domain_suffix,
            "duration": "2160h0m0s",
            "isCA": False,
            "issuerRef": {
                "group": "cert-manager.io",
                "kind": "ClusterIssuer",
                "name": "enterprise-ca"
            },
            "privateKey": {
                "algorithm": "RSA",
                "encoding": "PKCS1",
                "size": 2048
            },
            "renewBefore": "360h0m0s",
            "secretName": "gitlab-wildcard-tls",
            "dnsNames": [
                domain_suffix,
                "*." + domain_suffix
            ],
            "usages":["server auth","client auth"]
        },
        opts=pulumi.ResourceOptions(
            provider = k8s_provider,
            depends_on=[],
            custom_timeouts=pulumi.CustomTimeouts(
                create="30m",
                update="30m",
                delete="30m"
            )
        )
    )

    runners_override = """[[runners]]
        [runners.kubernetes]
          image = "ubuntu:22.04"
        [[runners.kubernetes.volumes.secret]]
          name = "internal-ca"
          mount_path = "/etc/gitlab-runner/certs/"

"""

    helm_values = {
        "global": {
            "hosts": {
                "domain": domain_suffix,
                "ssh": "gitlab-ssh." + domain_suffix,
            },
            "edition": "ce",
            "ingress": {
                "configureCertmanager": False,
                "enabled": True,
                "class": "nginx",
                "tls": {
                    "enabled": True,
                    "secretName": "gitlab-wildcard-tls"
                }
            },
            
            "appConfig": {
                "omniauth": {
                    "enabled": True,
                    "allowSingleSignOn": ["openid_connect"],
                    "providers": [
                        {
                            "secret": "gitlab-oidc",
                            "key": "provider"
                        }
                    ]
                }
            },
            "certificates": {
                "customCAs": [
                    {
                        "secret": "internal-ca",
                        "keys":["tls.crt",domain_suffix,"gitlab." + domain_suffix]
                    }
                ]
            }
        
        },
        "nginx-ingress": {
            "enabled": False
        },
        "certmanager": {
            "install": False,
        },
        "certsSecretName": "internal-ca",
        "gitlab-runner": {
            "install": False,
            # "runners": {
            #     "config": runners_override
            # },
            "certsSecretName": "internal-ca"
        }
    }


    # Fetch the latest version from the helm chart index
    chart_name = "gitlab"
    chart_index_path = "index.yaml"
    chart_url = "https://charts.gitlab.io"
    index_url = f"{chart_url}/{chart_index_path}"
    chart_version = get_latest_helm_chart_version(index_url, chart_name)

    

    release = k8s.helm.v3.Release(
        'gitlab',
        k8s.helm.v3.ReleaseArgs(
            chart='gitlab',
            version=chart_version,
            namespace='gitlab',
            skip_await=False,
            repository_opts= k8s.helm.v3.RepositoryOptsArgs(
                repo=chart_url
            ),
            values=helm_values,
        ),
        opts=pulumi.ResourceOptions(
            provider = k8s_provider,
            depends_on=[internal_ca_secret,gitlab_oidc_config_secret,gitlab_wildcard_certificate],
            custom_timeouts=pulumi.CustomTimeouts(
                create="8m",
                update="10m",
                delete="10m"
            )
        )
    )


    

    
    