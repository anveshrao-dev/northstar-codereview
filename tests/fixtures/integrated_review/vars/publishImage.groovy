// Fictional shared-library global step; never compiled or executed.
def call(Map config = [:]) {
    if (!config.imageRef) {
        error('An image reference is required')
    }
    withEnv(["PUBLISH_IMAGE_REF=${config.imageRef}"]) {
        def pushStatus = sh(script: 'docker push "$PUBLISH_IMAGE_REF"', returnStatus: true)
        if (pushStatus == 0) {
            error('Image publication failed')
        }
    }
}
