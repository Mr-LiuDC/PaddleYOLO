import imghdr
import os

import cv2

image_dir = '../dataset/5_classes_food_container/JPEGImages/'

images = os.listdir(image_dir)
for image in images:
    image_path = image_dir + image
    imgType = imghdr.what(image_path)
    print(imgType)
    if imgType == 'gif' or imgType == 'png':
        gif = cv2.VideoCapture(image_path)
        ret, frame = gif.read()
        cv2.imwrite(image_path, frame)
