def isMain() {
  def branch = env.BRANCH_NAME ?: env.GIT_BRANCH ?: ''
  return !env.CHANGE_ID && branch in ['main', 'origin/main', 'refs/heads/main']
}

pipeline {
  agent any

  environment {
    SSH_CONFIG_NAME = 'SEJONG-NAS'
    DEPLOY_DIR = '/volume1/docker/live-rec'
  }

  options {
    timestamps()
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }

  stages {
    stage('Validate') {
      steps {
        sh '''
          set -eu
          [ -f .env ] || : > .env
          docker compose config -q
          sh -n scripts/deploy.sh
        '''
      }
    }

    stage('Build') {
      // main is built by deploy.sh on the NAS; other branches only prove the image builds.
      when { not { expression { isMain() } } }
      steps {
        sh '''
          set -eu
          tag="live-rec-ci:$(printf '%s' "$BUILD_TAG" | tr -c 'A-Za-z0-9_.-' '-' | cut -c1-120)"
          trap 'docker image rm "$tag" >/dev/null 2>&1 || true' EXIT
          docker build -q -t "$tag" .
        '''
      }
    }

    stage('Deploy') {
      when { expression { isMain() } }
      steps {
        // The NAS checkout is the deployment: fast-forward it to the built commit (refusing local edits),
        // then deploy.sh rebuilds and restarts the container.
        sshPublisher(
          failOnError: true,
          publishers: [
            sshPublisherDesc(
              configName: env.SSH_CONFIG_NAME,
              verbose: true,
              transfers: [
                sshTransfer(
                  execTimeout: 1800000,
                  execCommand: """
                    set -eu; cd "${env.DEPLOY_DIR}"; git diff --quiet HEAD || { echo 'deploy: NAS checkout has local changes' >&2; git status --short >&2; exit 1; }; prev=\$(git rev-parse HEAD); git fetch -q origin main; git merge -q --ff-only "${env.GIT_COMMIT}"; sh scripts/deploy.sh "\$prev"
                  """.stripIndent().trim()
                )
              ]
            )
          ]
        )
      }
    }
  }
}
