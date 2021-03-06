#
# Copyright IBM Corp. 2016 All Rights Reserved.
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
#

import os, time, re, requests

from bdd_rest_util import buildUrl, CORE_REST_PORT
from bdd_json_util import getAttributeFromJSON
from bdd_test_util import cli_call, bdd_log

class ContainerData:
    def __init__(self, containerName, ipAddress, envFromInspect, composeService):
        self.containerName = containerName
        self.ipAddress = ipAddress
        self.envFromInspect = envFromInspect
        self.composeService = composeService

    def getEnv(self, key):
        envValue = None
        for val in self.envFromInspect:
            if val.startswith(key):
                envValue = val[len(key):]
                break
        if envValue == None:
            raise Exception("ENV key not found ({0}) for container ({1})".format(key, self.containerName))
        return envValue

    def __str__(self):
        return "{} - {}".format(self.containerName, self.ipAddress)

    def __repr__(self):
        return self.__str__()

def getDockerComposeFileArgsFromYamlFile(compose_yaml):
    parts = compose_yaml.split()
    args = []
    for part in parts:
        args = args + ["-f"] + [part]
    return args

def parseComposeOutput(context):
    """Parses the compose output results and set appropriate values into context.  Merges existing with newly composed."""
    # Use the prefix to get the container name
    containerNamePrefix = os.path.basename(os.getcwd()) + "_"
    containerNames = []
    for l in context.compose_error.splitlines():
        tokens = l.split()
        bdd_log(tokens)
        if 1 < len(tokens):
            thisContainer = tokens[1]
            if containerNamePrefix not in thisContainer:
               thisContainer = containerNamePrefix + thisContainer + "_1"
            if thisContainer not in containerNames:
               containerNames.append(thisContainer)

    bdd_log("Containers started: ")
    bdd_log(containerNames)
    # Now get the Network Address for each name, and set the ContainerData onto the context.
    containerDataList = []
    for containerName in containerNames:
        output, error, returncode = \
            cli_call(["docker", "inspect", "--format",  "{{ .NetworkSettings.IPAddress }}", containerName], expect_success=True)
        bdd_log("container {0} has address = {1}".format(containerName, output.splitlines()[0]))
        ipAddress = output.splitlines()[0]

        # Get the environment array
        output, error, returncode = \
            cli_call(["docker", "inspect", "--format",  "{{ .Config.Env }}", containerName], expect_success=True)
        env = output.splitlines()[0][1:-1].split()

        # Get the Labels to access the com.docker.compose.service value
        output, error, returncode = \
            cli_call(["docker", "inspect", "--format",  "{{ .Config.Labels }}", containerName], expect_success=True)
        labels = output.splitlines()[0][4:-1].split()
        dockerComposeService = [composeService[27:] for composeService in labels if composeService.startswith("com.docker.compose.service:")][0]
        bdd_log("dockerComposeService = {0}".format(dockerComposeService))
        bdd_log("container {0} has env = {1}".format(containerName, env))
        containerDataList.append(ContainerData(containerName, ipAddress, env, dockerComposeService))
    # Now merge the new containerData info with existing
    newContainerDataList = []
    if "compose_containers" in context:
        # Need to merge I new list
        newContainerDataList = context.compose_containers
    newContainerDataList = newContainerDataList + containerDataList

    setattr(context, "compose_containers", newContainerDataList)
    bdd_log("")

def allContainersAreReadyWithinTimeout(context, timeout):
    timeoutTimestamp = time.time() + timeout
    formattedTime = time.strftime("%X", time.localtime(timeoutTimestamp))
    bdd_log("All containers should be up by {}".format(formattedTime))

    allContainers = context.compose_containers

    for container in allContainers:
        if not containerIsInitializedByTimestamp(container, timeoutTimestamp):
            return False

    peersAreReady = peersAreReadyByTimestamp(context, allContainers, timeoutTimestamp)

    if peersAreReady:
        bdd_log("All containers in ready state, ready to proceed")

    return peersAreReady

def containerIsInitializedByTimestamp(container, timeoutTimestamp):
    while containerIsNotInitialized(container):
        if timestampExceeded(timeoutTimestamp):
            bdd_log("Timed out waiting for {} to initialize".format(container.containerName))
            return False

        bdd_log("{} not initialized, waiting...".format(container.containerName))
        time.sleep(1)

    bdd_log("{} now available".format(container.containerName))
    return True

def timestampExceeded(timeoutTimestamp):
    return time.time() > timeoutTimestamp

def containerIsNotInitialized(container):
    return not containerIsInitialized(container)

def containerIsInitialized(container):
    isReady = tcpPortsAreReady(container)
    isReady = isReady and restPortRespondsIfContainerIsPeer(container)

    return isReady

def tcpPortsAreReady(container):
    netstatOutput = getContainerNetstatOutput(container.containerName)

    for line in netstatOutput.splitlines():
        if re.search("ESTABLISHED|LISTEN", line):
            return True

    bdd_log("No TCP connections are ready in container {}".format(container.containerName))
    return False

def getContainerNetstatOutput(containerName):
    command = ["docker", "exec", containerName, "netstat", "-atun"]
    stdout, stderr, returnCode = cli_call(command, expect_success=False)

    return stdout

def restPortRespondsIfContainerIsPeer(container):
    containerName = container.containerName
    command = ["docker", "exec", containerName, "curl", "localhost:{}".format(CORE_REST_PORT)]

    if containerIsPeer(container):
        stdout, stderr, returnCode = cli_call(command, expect_success=False)

        if returnCode != 0:
            bdd_log("Connection to REST Port on {} failed".format(containerName))

        return returnCode == 0

    return True

def peersAreReadyByTimestamp(context, containers, timeoutTimestamp):
    peers = getPeerContainers(containers)
    bdd_log("Detected Peers: {}".format(peers))

    for peer in peers:
        if not peerIsReadyByTimestamp(context, peer, peers, timeoutTimestamp):
            return False

    return True

def getPeerContainers(containers):
    peers = []

    for container in containers:
        if containerIsPeer(container):
            peers.append(container)

    return peers

def containerIsPeer(container):
    # This is not an ideal way of detecting whether a container is a peer or not since
    # we are depending on the name of the container. Another way of detecting peers is
    # is to determine if the container is listening on the REST port. However, this method
    # may run before the listening port is ready. Hence, as along as the current
    # convention of vp[0-9] is adhered to this function will be good enough.
    return re.search("vp[0-9]+", container.containerName, re.IGNORECASE)

def peerIsReadyByTimestamp(context, peerContainer, allPeerContainers, timeoutTimestamp):
    while peerIsNotReady(context, peerContainer, allPeerContainers):
        if timestampExceeded(timeoutTimestamp):
            bdd_log("Timed out waiting for peer {}".format(peerContainer.containerName))
            return False

        bdd_log("Peer {} not ready, waiting...".format(peerContainer.containerName))
        time.sleep(1)

    bdd_log("Peer {} now available".format(peerContainer.containerName))
    return True

def peerIsNotReady(context, thisPeer, allPeers):
    return not peerIsReady(context, thisPeer, allPeers)

def peerIsReady(context, thisPeer, allPeers):
    connectedPeers = getConnectedPeersFromPeer(context, thisPeer)

    if connectedPeers is None:
        return False

    numPeers = len(allPeers)
    numConnectedPeers = len(connectedPeers)

    if numPeers != numConnectedPeers:
        bdd_log("Expected {} peers, got {}".format(numPeers, numConnectedPeers))
        bdd_log("Connected Peers: {}".format(connectedPeers))
        bdd_log("Expected Peers: {}".format(allPeers))

    return numPeers == numConnectedPeers

def getConnectedPeersFromPeer(context, thisPeer):
    url = buildUrl(context, thisPeer.ipAddress, "/network/peers")
    response = requests.get(url, headers={'Accept': 'application/json'}, verify=False)

    if response.status_code != 200:
        return None

    return getAttributeFromJSON("peers", response.json(), "There should be a peer json attribute")