import xml.etree.ElementTree as ET

# https://blog.csdn.net/weixin_41278720/article/details/84872064

anno_path = 'C:\\Users\\LiuDecai\\Downloads\\5_types_of_containers\\Annotations\\10013independent.xml'

tree = ET.parse(anno_path)

root = tree.getroot()

labels = []
for obj in root.iter('object'):
    name = obj.find('name').text
    labels.append(name)

print(labels)

size = root.find('size')
width = float(size.find('width').text)
height = float(size.find('height').text)
print(width, height)
