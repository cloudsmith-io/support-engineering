# Docker Multi-Arch images overview script

The script will return an overview of your Docker multi-arch images stored in your Cloudsmith repository. 
It provides a hierarchial breakdown of each image by tag, showing the index digest and it's associated manifest digests with their platform, cloudsmith sync status and downloads count. 

Each image has a total downloads count rolled up from all digests which the current Cloudsmith UI/ API does not provide. 

<img src="example.gif" width=50%>

## Prequisities

Configure the Cloudsmith environment variable with your PAT or Service Account Token. 

    export CLOUDSMITH_API_KEY=<api-key>


## How to use

Execute run.sh and pass in 4 arguements ( domain, org, repo and image name).

        ./run.sh colinmoynes-test-org docker library/golang 

* if not using a custom domain, you can simply pass in an empty string "" as the first param


## So, how does this work?

## Get matching tags

 * Fetch all tags via the Docker v2 /tags/list endpoint using the image name e.g. library/nginx


### For each tag

* Pass the tag into manifests/ endpoint and return json for the manifest/list file. 
* Read the json and parse out the digests
* Total downloads count value incremented from child manifests

#### For each digest

* Iterate through the digests
* Fetch the platform and os data from the manifest json
* Lookup the digest (version) via the Cloudsmith packages list endpoint using query string. 
* Fetch the sync status and downloads count values
* Increment the total downloads value


