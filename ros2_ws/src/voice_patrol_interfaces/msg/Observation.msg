# One observation, supplied by a sensor or explicitly marked software fixture.
string<=128 label
float32 confidence
string<=128 sensor
string<=2048 media_uri
# Image-space x/y/width/height. Zeroes when no image bounding box applies.
float32[4] bbox_xywh
# Bounded plugin metadata. JSON object; must not contain NaN or infinity.
string<=16384 details_json
