pipeline {
    agent { dockerfile true }

    environment {
        HOME = "${WORKSPACE}"
    }

    stages {
        stage('Fetch') {
            steps {
                sh 'rm -rf src && git clone --depth 1 "$REPO_URL" src'
            }
        }
        stage('Build') {
            steps {
                sh 'python3.11 nuitka_build.py "src/$ENTRY"'
            }
        }
    }

    post {
        success {
            archiveArtifacts artifacts: 'build/*'
        }
    }
}