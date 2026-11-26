# Change log

## 8.0.0

### Breaking Changes

* Redis editor storage (`docservice.editorDataStorage`, `docservice.editorStatStorage`) and Redis TLS (`connections.redisTLS`, `connections.redisSentinelTLS`) require a DocumentServer release with Redis editor storage support (entrypoint and server). Older images ignore these settings. The chart rejects images older than the minimum supported version once that version is set
* Removed the `connections.redisConnectorName` parameter (the `ioredis` switch for Redis Sentinel). Redis Sentinel is now used when `docservice.editorDataStorage` or `docservice.editorStatStorage` is `sentinel` and `connections.redisSentinelNodes` is set, and Redis Cluster when it is `cluster` and `connections.redisClusterNodes` is set; the nodes alone do nothing in memory mode. Remove `redisConnectorName` from your values files. Setting it to `ioredis` (or any value other than `redis`) now fails the install/upgrade with a migration message; `redis` is ignored with a notice
* `REDIS_SENTINEL_PWD` is now rendered only if `connections.redisSentinelNodes` is set and `connections.redisSentinelNoPass` is `false` (the default is `true`), and `REDIS_SERVER_PWD` is no longer rendered if `connections.redisNoPass` is `true`. Users who set `redisConnectorName: ioredis` with an authenticated Sentinel must set `connections.redisSentinelNodes`, `connections.redisSentinelGroupName` and `connections.redisSentinelNoPass: false`

### New Features

* Added render-time validation, active only when Redis storage is used: Redis Cluster requires `connections.redisDBNum` `0`, `connections.redisCAExistingSecret` requires `connections.redisTLS` or `connections.redisSentinelTLS`, and `connections.redisDBNum` must be a non-negative integer
* Added a post-install warning (`NOTES.txt`) for in-memory editor storage with more than one DocService replica or with autoscaling, because the co-editing state is per Pod and the chart has no document-affinity routing
* Added a DocumentServer version gate for Redis editor storage (`ds.redis.minImageVersion` in `templates/_helpers.tpl`). It rejects known older numeric `docservice` image tags when a Redis storage mode is selected and is inactive until the minimum DocumentServer version is set
* Added configurable editor storage: `docservice.editorDataStorage` (`memory`, `standalone`, `sentinel` or `cluster`, default `memory`) and `docservice.editorStatStorage` (defaults to the value of `editorDataStorage`). The chart fails to render for unsupported modes and for conflicting Redis topologies
* Added Redis and Sentinel TLS support (`connections.redisTLS`, `connections.redisSentinelTLS`) and a shared private CA bundle (`connections.redisCAExistingSecret`, `connections.redisCASecretKeyName`) mounted into the Docservice, Adminpanel and test Pods. Requires a DocumentServer image with the Redis TLS changes
* Added password-only and named ACL user authentication for Redis and Sentinel (`connections.redisUser: ""` omits the username, new `connections.redisSentinelUser`)
* Added `tests.extraEnvVars`, `tests.extraVolumes` and `tests.extraVolumeMounts` for the test Pod
* Added example values files `demo/my-values.redis-standalone.yml`, `demo/my-values.redis-data-only.yml`, `demo/my-values.redis-sentinel.yml` and `demo/my-values.redis-cluster.yml`, and a `test-redis-config` test (`test_redis_config.sh`). `helm test` runs it for any Redis-backed storage mode: it checks that the DocService image selects the requested storage and topology, and fails fast on images without Redis storage support. Connectivity, authentication and TLS are checked by `test-ds`
* Added demo manifests `demo/redis-sentinel.yaml` and `demo/redis-cluster.yaml` (Valkey, without TLS) for testing the Sentinel and Cluster editor storage modes and failover; the Sentinel and Cluster values files now point to them
* Added optional TLS to the demo Sentinel and Cluster test installs: `demo/redis-tls-certs.yaml` has cert-manager resources (a self-signed test CA, a CA Issuer and a server Certificate issued into the Secret `redis-tls`), the manifests switch to TLS when `redis-tls` exists, and `demo/my-values.redis-tls.yml` enables `redisTLS`, `redisSentinelTLS` and `redisCAExistingSecret` (the chart mounts only the `ca.crt` item of that Secret)
* Added CI for the chart: the `chart-unit-tests` job in `lint.yaml` runs the rendering and connectivity-hook unit tests and `tests/entrypoint_contract.py` (the real DocumentServer entrypoint against the environment of each component, pinned DocumentServer commit), and the weekly `entrypoint-drift` workflow runs the contract against DocumentServer `main`; the test files are excluded from the chart package
* Documented how to switch an existing release to Redis TLS without failing the `pre-upgrade` Job (README, "Switching an existing release to Redis TLS") and a controlled Cluster failover test with `CLUSTER FAILOVER` (`demo/Readme.md`)

### Changes

* Removed `REDIS_CONNECTOR_NAME` from the shared `documentserver` ConfigMap: the DocumentServer entrypoint defaults it to `redis`, the only connector the server accepts. `REDIS_SENTINEL_TLS` is now rendered only with `connections.redisSentinelNodes` or when `connections.redisSentinelTLS` is true (a missing value means false, and `true` also applies to a Sentinel configured in local.json or NODE_CONFIG). All Pods that use the ConfigMap restart once on upgrade, as they already do for other 8.0.0 changes
* The Redis flags `connections.redisTLS`, `connections.redisSentinelTLS`, `connections.redisNoPass` and `connections.redisSentinelNoPass` are parsed like the DocumentServer entrypoint does (`true`/`yes`/`y`/`on`/`1` and `false`/`no`/`n`/`off`/`0`, case-insensitive; empty means unset). Before, the string `"false"` (for example from `--set-string`) was taken as true: `redisNoPass: "false"` dropped the password and `redisTLS: "false"` mounted the CA with TLS off. Other values now fail the render. The editor storage modes are computed in one place (`ds.editor.dataStorage` and `ds.editor.statStorage`); renders for boolean values are unchanged
* The Converter no longer receives Redis credentials or the Redis CA: it does not use Redis. Only the Docservice, Adminpanel and the test Pods do. The Redis usernames (`REDIS_SERVER_USER`, `REDIS_SENTINEL_USER`) moved from the shared `documentserver` ConfigMap to the env of the Redis-using Pods, because the entrypoint rejects a Sentinel user without a password. All Pods that use the ConfigMap and the Converter Pods restart once on upgrade
* The Redis password environment variables are now rendered by shared helpers in all workloads and in the test Pod
* The test Pod selects the Redis mode from `REDIS_SENTINEL_NODES` and `REDIS_CLUSTER_NODES` instead of `REDIS_CONNECTOR_NAME`
* README: documented the Redis storage topologies, TLS and authentication secrets; fixed the parameter name `connections.redistHost` to `connections.redisHost`

### Fixes

* Clarified that `connections.redisHost` and `connections.redisPort` are used only for standalone Redis and are ignored when Sentinel or Cluster nodes are set
* Fixed `helm test` reporting success when a check other than `CheckDS` failed. Skipped checks do not fail the run
* Memory editor storage (the default) no longer requires Redis: the Redis password env, Secrets, CA wiring and password validation are rendered only when `editorDataStorage` or `editorStatStorage` is a Redis mode. `test-ds` reports `CheckRedis` as `Skipped` otherwise
* The `test-ds` Redis check now writes a temporary `ds:helm-test:<uuid>` key with a 60 second expiry, so ACL users restricted to `~ds:*` work
* `test-ds` now uses all configured Sentinel nodes and all Cluster nodes instead of only the first one, so `helm test` no longer fails when that node is down
* The `test-ds` RabbitMQ check declares an exclusive queue instead of a transient non-exclusive one, which RabbitMQ 4.3 rejects (`transient_nonexcl_queues` is a deprecated feature), so `helm test` passes with RabbitMQ 4.3 (and 3.13)
* The post-install notes show the log command of the `test-redis-config` test Pod when Redis editor storage is used

## 7.0.0

### Breaking Changes

* The `pre-delete` Job is now disabled by default (`delete.job.enabled`). It drops the `doc_changes`, `task_result` and `doc_callbacks` tables, and with in-memory editor state the wait for open documents only counts the documents on the Pod that received the request, so the tables may be dropped while other Pods are still saving
* Removed the `privateCluster` and `sqlScripts.branchName` parameters
* The defaults of `*.existingConfigmap.tblCreate.name` and `*.existingConfigmap.tblRemove.name` are now empty. The ConfigMap is mounted only if the name is set

### Changes

* Pinned `images.tag` to `v9.3.8` and `appVersion` to 9.3.8. `cluster-utils` 9.3.8+ is required because it bundles `/scripts/schema`
* The database Jobs now use the SQL scripts bundled in the `cluster-utils` image under `/scripts/schema/<dbType>/` instead of downloading them at runtime

### Fixes

* Fixed the `pre-install` Job silently creating no tables when the SQL script could not be downloaded. A missing script now fails the Job
* Fixed the `pre-rollback` ConfigMap staying in the namespace after a successful rollback, which blocked the next rollback
* Fixed the `pre-upgrade` and `pre-rollback` Jobs running `createdb.sql` and reporting success after `removetbl.sql` failed

## 6.1.0

### New Features

* Added the ability to set `revisionHistoryLimit` for the Docservice, Converter, Adminpanel and Example workloads (`docservice.revisionHistoryLimit`, `converter.revisionHistoryLimit`, `adminpanel.revisionHistoryLimit`, `example.revisionHistoryLimit`)
* Added the ability to take the AMQP username from an existing Secret (`connections.amqpUserExistingSecret`, `connections.amqpUserSecretKeyName`); when set, `AMQP_USER` is no longer rendered in the `documentserver` ConfigMap
* Added `checksum/*` annotations to the Docservice, Converter, Adminpanel and Example Pod templates, so the Pods are restarted when the ConfigMaps and Secrets rendered by the chart change
* Added `customResources.versionSalt` to force a new `DS_VERSION_HASH` after the content of the custom resources PVC changes
* Added the `resume-server` job (`resumeServer.*`), a `post-upgrade`/`post-rollback` hook that switches the document server back on (`DELETE /internal/cluster/inactive`) after the `pre-upgrade`/`pre-rollback` jobs switched it off, and verifies the result. It is needed because the Pods are no longer restarted on every upgrade
* Added a version gate to the `pre-upgrade` and `pre-rollback` jobs (`upgrade.job.skipWhenUnchanged`, `rollback.job.skipWhenUnchanged`, default `true`). The server is switched off and the database tables are reset only if the docservice or converter tag or the database connection changes, or if the running version differs from the target
* README: upgrade lifecycle, Helm hooks under Argo CD and manual recovery (section 13)

### Changes

* **Behavior change:** the default of `docservice.podAnnotations`, `converter.podAnnotations`, `adminpanel.podAnnotations` and `example.podAnnotations` is now `{}` instead of `rollme: "{{ randAlphaNum 5 | quote }}"`. Pods are no longer restarted on every `helm upgrade`, only when the hashed ConfigMaps/Secrets or the image change. Secrets and ConfigMaps not rendered by the chart (`*ExistingSecret`, WOPI keys, `extraConf`, `extraThemes`) and the custom resources PVC content require Stakater Reloader, versioned names or a change of `customResources.versionSalt`. The previous behavior can be restored by setting `rollme: "{{ randAlphaNum 5 | quote }}"` in `*.podAnnotations`
* **Behavior change:** a `helm upgrade` or `helm rollback` that changes neither the image tags nor the database connection no longer switches the server off and no longer resets the database tables. Set `upgrade.job.skipWhenUnchanged=false` (`rollback.job.skipWhenUnchanged=false`) to get the previous behavior
* `DS_VERSION_HASH` is no longer derived from the current time. It is computed from the image tag, the `customFonts` / `customDictionaries` / `customPlugins` settings and `customResources.versionSalt`, so the rendered output is deterministic (required for Argo CD)

### Fixes

* Fixed perpetual OutOfSync and unneeded rollouts under Argo CD caused by `rollme` and the time-based `DS_VERSION_HASH`
* Fixed the document server staying switched off after an upgrade or a rollback once the `rollme` restart was removed

## 6.0.0

### New Features

* Added the ability to add custom fonts, plugins, and dictionaries without rebuilding container images
* Added the ability to use a single common image for running ONLYOFFICE Docs services
* Added a common image tag for ONLYOFFICE Docs services and jobs
* Added a separate field for selector labels

### Changes

* Released ONLYOFFICE Docs [v9.3.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#931)
* ONLYOFFICE Docs services are now run from a single common container image
* Updated the mechanism used to start ONLYOFFICE Docs services
* Updated and aligned the installation process for OpenShift environments

### Fixes

* Fixed an upgrade issue occurring when dynamic values were used in `commonLabels`

## 5.3.1

### Changes

* Released ONLYOFFICE Docs [v9.2.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#921)
* Changed the base image to Fedora

### Fixes

* Fixed a bug with starting services on ARM

## 5.3.0

### Changes

* Released ONLYOFFICE Docs [v9.2.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#920)

### Fixes

* Fixed the lack of WOPI keys when enabling WOPI in the Admin panel

## 5.2.0

### New Features

* Added Admin panel service
* Added the ability to connect Oracle Thick Mode

### Changes

* Released ONLYOFFICE Docs [v9.1.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#910)

## 5.1.1

### Changes

* Released ONLYOFFICE Docs [v9.0.4](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#904)

## 5.1.0

### New Features

* Added the ability to specify multiple hostname for Ingress
* Added the ability to set up the certificate request in Let's Encrypt
* Added the ability to use custom user dictionaries

### Changes

* Released ONLYOFFICE Docs [v9.0.3](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#903)

## 5.0.0

### New Features

* Added the ability to connect to multiple Redis Sentinel nodes
* Added the creation of a PVC to store a shared runtime config. You can specify an existing PVC
* Added the ability to specify an existing Secret for Proxy `secure_link`
* Added support for Ingress [NGINX Ingress by NGINX](https://github.com/nginx/kubernetes-ingress/) and [HAProxy](https://github.com/haproxytech/kubernetes-ingress/) for virtual path connection
* Added the ability to connect via Openshift route

### Changes

* Released ONLYOFFICE Docs [v9.0.2](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#902)
* The connection configuration via the virtual path has been [changed](https://github.com/ONLYOFFICE/Kubernetes-Docs#5325-expose-onlyoffice-docs-on-a-virtual-path)

## 4.7.0

### New Features

* Added the ability to connect to Dameng databases
* Added the ability to set up the `hostAliases`

### Changes

* Released ONLYOFFICE Docs [v8.3.3](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#833)

## 4.6.0

### New Features

* Added the ability to connect to Oracle and MS SQL Server databases

### Changes

* Released ONLYOFFICE Docs [v8.3.2](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#832)

## 4.5.1

### Changes

* Released ONLYOFFICE Docs [v8.3.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#831)

## 4.5.0

### New Features

* Added the ability to automatically generate WOPI keys during installation or upgrades, or to use existing ones
* Added the ability to modify the names of created resources by appending a specified suffix

### Changes

* The values of the parameters `jwt.secret`, `proxy.secureLinkSecret`, and `proxy.infoAllowedPassword` have been replaced with empty values instead of default ones, with the option to either generate random values or use the provided value
* Released ONLYOFFICE Docs [v8.3.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#830)

## 4.4.1

### Changes

* Released ONLYOFFICE Docs [v8.2.2](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#822)

## 4.4.0

### New Features

* Added the ability to connect to Redis Sentinel nodes with a password
* Added the ability to set additional custom env variables for containers
* Added the ability to add additional custom volumes for containers
* Added the ability to set up Nginx log format in the Proxy container

### Changes

* Released ONLYOFFICE Docs [v8.2.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#821)

## 4.3.0

### New Features

* Added a signal control processing handler as the first process in Docs containers
* Added the ability to set up the `terminationGracePeriodSeconds`
* Added the ability to generate a hash that will be appended to the ONLYOFFICE Docs version in the request URLs

### Changes

* Released ONLYOFFICE Docs [v8.2.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#820)

## 4.2.2

### Changes

* Released ONLYOFFICE Docs [v8.1.3](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#813)

## 4.2.1

### Changes

* Changed the default value for `converter.updateStrategy.type` to `Recreate`
* Released ONLYOFFICE Docs [v8.1.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#811)

### Fixes

* Fixed a bug related to cases when the installation/update continued with database tables recreation errors, an additional check of database availability is added in Jobs scripts

## 4.2.0

### New Features

* Added the ability set up the `containerSecurityContext`
* Added the ability to set up its own `nodeSelector` and `tolerations` for each service or define them globally
* Added the ability to separate a subdirectory for storing cache (data) and service (errors and forgotten) files
* Actions to clean the directory ".../cache/files/" are placed in a separate Job with a `post-upgrade` hook
* Added the ability to set up container `lifecycle` hooks

### Changes

* Changed the default values for `podSecurityContext`
* In `pre-upgrade` Job, the actions for cleaning the directory ".../cache/files/" have been removed
* Released ONLYOFFICE Docs [v8.1.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#810)

## 4.1.0

### New Features

* Added the ability set up annotations for all the deployed resources
* Added the ability to disable  `PVC` creation when using the S3 storage

### Changes

* Using an already configured image for the `helm test` command
* Released [v8.0.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#801) of ONLYOFFICE Docs

### Fixes

* Fixed a bug when adding multiline annotations to Ingress

## 4.0.0

### New Features

* Added the ability to connect to Redis Cluster 
* Added the ability to set up custom `podAntiAffinity`
* Added the ability to set up max size uploaded file
* Added the ability to map config file to Example

### Changes

* Updated the Security Context Constraints policy for OpenShift
* Released [v8.0.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#800) of ONLYOFFICE Docs

## 3.5.0

### New Features

* Added the ability to restrict access to the info page

## 3.4.1

### Changes

* Released [v7.5.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#751) of ONLYOFFICE Docs

## 3.4.0

### Changes

* Released [v7.5.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#750) of ONLYOFFICE Docs

## 3.3.0

### New Features

* Added the ability to connect to the ONLYOFFICE Docs via a virtual path
* Added the ability to connect to Redis Sentinel 

### Changes

* Released [v7.4.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#741) of ONLYOFFICE Docs

### Fixes

* Fixed Ingress Class definition

## 3.2.0

### New Features

* Added the ability to use private IP addresses to connect to ONLYOFFICE Docs

### Changes

* Released [v7.4.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#740) of ONLYOFFICE Docs

### Fixes

* Fixed ONLYOFFICE Docs crash in k8s cluster without ipv6
* Fixed an error with the Api Version when running Horizontal Pod Autoscaling in k8s cluster v1.26+

## 3.1.0

### New Features

* Added `helm test` for ONLYOFFICE Docs launch testing and connected dependencies availability testing
* Added `NOTES.txt`
* Added the ability set up `imagePullSecrets`

### Fixes

* Fixed variable handling for DocumentServer metrics

## 3.0.2

### Changes

* Released [v7.3.3](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#733) of ONLYOFFICE Docs

## 3.0.1

### Fixes

* Fixed a set of dashboards for Grafana

## 3.0.0

### New Features

* Added the ability set up session affinity for the "DocumentServer" Service
* Automated installation of ready-made Grafana dashboards

### Changes

* Released [v7.3.2](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#732) of ONLYOFFICE Docs
* Changed the Helm repository of the NFS Server Provisioner chart

### Fixes

* Fixed a bug with displaying solution type on the welcome page

## 2.2.0

### New Features

* Added the ability to disable the welcome page
* Added the ability set up custom Init Containers

### Changes

* Released [v7.2.2](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#722) of ONLYOFFICE Docs

## 2.1.0

### New Features

* Added additional parameters to connect to Redis
* Added the ability to specify an existing ServiceAccount or create a new one
* Added the ability set up custom `labels`, `nodeAffinity`, `podAffinity` and `namespace`

### Fixes

* Fixed service port in ONLYOFFICE Docs ingress

## 2.0.1

### New Features

* Added the ability to set up a list of IP addresses to access the Info page

### Changes

* Released [v7.2.1](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#721) of ONLYOFFICE Docs

## 2.0.0

### New Features

* Added the ability to use MySQL or MariaDB as a database server
* Added the ability set up `updateStrategy`
* Added the ability set up `containerPorts`

### Changes

* Released [v7.2.0](https://github.com/ONLYOFFICE/DocumentServer/blob/master/CHANGELOG.md#720) of ONLYOFFICE Docs
* Changed the syntax and algorithm for processing the following parameters: `Affinity`, `SecurityContext`, `Probes`, `Images`
* Changed the Redis version
* Changed the Welcome page

## 1.1.3

### Fixes

* Fixed the path to sql scripts

## 1.1.2

### Fixes

* Fixed running Jobs in an OpenShift cluster

## 1.1.1

### New Features

* Added the ability to disable the run of Jobs

### Fixes

* Fixed errors when connecting via the WOPI protocol
* Fixed the conversion error in Example

## 1.1.0

### New Features

* Added the ability to perform Jobs: `pre-upgrade`, `pre-rollback` and `pre-delete` in private k8s cluster
* Added `Job` `pre-install` (with support for execution in private cluster) to initialize the database before starting ONLYOFFICE Docs
* Added the ability to run ONLYOFFICE Docs in a private k8s cluster behind a Web Proxy

### Changes

* Changed the algorithm in the deletion script, which is executed in `Job` `pre-delete`
* Changed the image for the Jobs, which includes the utilities necessary for execution
* Changed the scripts executed by Jobs, taking into account the image change

## 1.0.1

### New Features

* Added the ability to specify an existing `Service` for "DocumentServer"

### Changes

* Release v7.1.1 of ONLYOFFICE Docs
* Added a variable to specify the port number of the `Redis` server in `ConfigMap` `documentserver`

### Fixes

* Fixed the name of the variable for specifying the host of the Redis server

## 1.0.0

### New Features

* Added the ability set up Horizontal Pod Autoscaling
* Added the ability set up `nodeSelector` and `tolerations`
* Added the ability set up annotations for ingress
* Added build of Helm Chart releases with their publication in the repository
* Added a description of the DocumentServer metrics that are visualized in Grafana

### Changes

* Release v7.1.0 of ONLYOFFICE Docs
* Changed the Helm Chart name from `documentserver` to `docs`
* Changed the Helm Chart versioning to the `SemVer` format
* Removed the instruction for updating the "DocumentServer" using a script

## 22.5.4

### New Features

* Added the ability to map a `ConfigMap` containing its own json file with interface themes
* Added the ability to specify different `JWT` parameters for inbox and outbox requests
* Added the ability to use `activemq` as an `AMQP` server
* Added the ability set up `annotations` for `Pods`
* Added the ability set up `annotations` for `Service`

## 22.4.15

### New Features

* Added the ability to specify the type and template of logging
* Added the ability to specify an existing `Secret` with `JWT` variables
* Added the ability to rollback `helm rollback` after updating "DocumentServer" via `helm upgrade`
* Added the ability to use existing `PVC` or `Secret` to store the license

### Changes

* Edits to the Helm Chart release deletion procedure
* Changed the Helm Chart versioning to the `CalVer` format

## 7.0.1

### New Features

* Added the ability to upgrade using `helm upgrade`
* Added the ability to map node-config files
* Added a `podAntiAffinity` policy for distributing `Pods` across the `Nodes` of the "k8s" cluster
* Added the ability to specify an existing `PVC` for "DocumentServer"
* Added the ability to specify existing `Secrets` containing passwords for `PostgreSQL` and `RabbitMQ`
* Added the ability to specify the DNS name when installing with Nginx Ingress Controller without certificates

### Changes

* Release v7.0.1 of ONLYOFFICE Docs
* Edited the instructions for installing metrics
* Improved the update and shutdown scripts
* Added the list of variables in `ConfigMap` `documentserver` and in `Secret` `jwt`

### Fixes

* Fixed the operation of `Example` when installing with Nginx Ingress Controller

## 7.0.0

### Changes

* Release v7.0.0 of ONLYOFFICE Docs

