pipeline {
    agent { label 'rocky-node' }

    parameters {
        string(name: 'REPO',   defaultValue: '', description: 'owner/repo')
        string(name: 'BRANCH', defaultValue: '', description: 'Branch name')
        string(name: 'ENTRY',  defaultValue: '', description: 'Entry .py path relative to the repository root')
        string(name: 'DATA',   defaultValue: '', description: 'Comma-separated data directories to bundle')
    }

    stages {
        stage('Build Image') {
            steps {
                sh 'rm -rf build src'
                sh 'docker build -t "nuitka-build:${BUILD_NUMBER}" "$WORKSPACE"'
            }
        }

        stage('Fetch') {
            steps {
                withCredentials([string(credentialsId: 'github-token', variable: 'GH_TOKEN')]) {
                    sh 'git clone --branch "$BRANCH" "https://x-access-token:$GH_TOKEN@github.com/$REPO.git" src'
                    sh 'git -C src remote set-url origin "https://github.com/$REPO.git"'
                }
            }
        }

        stage('Build') {
            steps {
                sh '''
                    ARGS=""
                    IFS=','
                    for d in $DATA; do
                        [ -n "$d" ] && ARGS="$ARGS --data-dir $d"
                    done
                    unset IFS

                    docker run --rm --user "$(id -u):$(id -g)" \
                      -e HOME=/tmp \
                      -v "$WORKSPACE:$WORKSPACE" -w "$WORKSPACE" "nuitka-build:${BUILD_NUMBER}" \
                      python3.11 /opt/nuitka/nuitka_build.py "src/$ENTRY" --auto-deps $ARGS
                '''
            }
        }
    }

    post {
        success {
            archiveArtifacts artifacts: 'build/*'
        }
    }
}
