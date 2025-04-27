ffplay -icy 1 -loglevel verbose "https://energyzuerich.ice.infomaniak.ch/energyzuerich-high.mp3"


ffmpeg -re -i "https://energyzuerich.ice.infomaniak.ch/energyzuerich-high.mp3" ^
-loop 1 -i "overlay_image.png" ^
-filter_complex "[1:v]scale=1280:720[bg]" ^
-map "[bg]" -map "0:a" -c:v libx264 -c:a aac -f matroska - | ffplay -

ffmpeg -re -i "https://energyzuerich.ice.infomaniak.ch/energyzuerich-high.mp3" -loop 1 -i overlay_image.png -shortest -c:v libx264 -preset ultrafast -tune stillimage -c:a aac -b:a 128k -vf scale=1280:720 -f matroska -listen 1 http://0.0.0.0:8090/live.mkv
