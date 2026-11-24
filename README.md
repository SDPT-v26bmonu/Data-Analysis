Make sure ArcGIS data is from this link 
https://serendpt.maps.arcgis.com/home/item.html?id=6141d2ed34c049ebb7125bc0614884c2#overview
Export the data as a "geodatabase" file
The File System should look as follows
Need to add /Data yourself
/Outputs and /Photos should be created and populated when running the "extract_and_organize_photos.py"
    /Data
    /Ouputs
    /Photos
    /Scripts
    .gitignore
    requirements.txt
From your downloads folder unzip the ArcGIS data into the /Data file
rename the unzipped folder "data.gdb"
install all the requirements in "requirements.txt"
run "extract_and_organize_photos.py"

When updating the data delete the contents of the /Data folder and unzip the updated data into the same format as before