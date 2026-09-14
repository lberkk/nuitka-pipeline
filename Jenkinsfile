pipeline {
    agent { label 'rocky-node' }
    
    stages {
        stage('Build Image') {
            steps {
            sh ''' 
            docker build -t nuitka-build:${BUILD_NUMBER} "$WORKSPACE"
        '''
            }
        }
        stage('Fetch') {
            steps {
                sh 'rm -rf build src'
                withCredentials([string(credentialsId: 'github-token', variable: 'GH_TOKEN')]) {
                    sh 'git clone --branch "$BRANCH" "https://x-access-token:$GH_TOKEN@github.com/$REPO.git" src'
                    sh 'git -C src remote set-url origin "https://github.com/$REPO.git"'
                }
            }
        }

        stage('Build') {
            steps {
                sh '''
            docker run --rm --user "$(id -u):$(id -g)" -v "$WORKSPACE:$WORKSPACE" -w "$WORKSPACE" nuitka-build:${BUILD_NUMBER} \
              python3.11 /opt/nuitka/nuitka_build.py "src/$ENTRY" $EXTRA --auto-deps
        '''
            }
        } 

    }

    post {
        success {
            archiveArtifacts(
                artifacts: 'build/*'
            )
        }
    }
}
